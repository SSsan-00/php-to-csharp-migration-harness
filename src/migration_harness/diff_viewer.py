from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from migration_harness.capsules import (
    input_encoding, load_capsule_manifest, migration_php_encodings, pinned_php_sources, read_pinned_php,
)
from migration_harness.config import target_paths
from migration_harness.files import sha256_file
from migration_harness.run_records import load_saved_run, validate_run_id
from migration_harness.worktree_targets import worktree_source_lock


def _inside_file(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError(f"比較に必要なファイルを参照できません: {relative}")
    return path


def _file_version(path: Path) -> tuple[Any, ...] | None:
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return (path.resolve(), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _pane(files: list[tuple[str, str]]) -> tuple[str, list[dict[str, Any]]]:
    """複数ファイルを連結し、Viewerが元のファイルと行へ戻れる境界情報を作る。"""
    parts = []
    segments = []
    start = 1
    for index, (name, text) in enumerate(files, 1):
        ends_with_newline = text.endswith("\n")
        last = index == len(files)
        # Viewerに合わせ、途中のファイルの末尾改行は次ファイルとの境界として扱う。
        line_count = max(1, text.count("\n") + (last or not ends_with_newline))
        segments.append({
            "fileName": name, "fileIndex": index, "startLine": start,
            "lineCount": line_count, "endsWithNewline": ends_with_newline,
        })
        parts.append(text if last or ends_with_newline else text + "\n")
        start += line_count
    return "".join(parts), segments


def capture_workspace(
    config: dict[str, Any], run_id: str, *, focus: dict[str, Any] | None = None,
    target_hashes: dict[str, str] | None = None, include_draft: bool = False,
) -> tuple[Path, dict[str, Any]]:
    """Runの参照PHPと移行先をそろえて読み、比較時点の内容を固定する。"""
    validate_run_id(run_id)
    with worktree_source_lock(Path(config["worktree_root"]) / run_id):
        run_path, worktree, record, unit = load_saved_run(config, run_id)
        if record.get("target_apply_pending"):
            raise ValueError("移行先の反映が途中です。同じRun IDで再開してから比較してください。")
        capsule_path, manifest = load_capsule_manifest(run_path)
        encodings = migration_php_encodings(manifest)
        if not encodings:
            raise ValueError("この実行の移行対象にPHPがありません。")
        pinned = pinned_php_sources(manifest)
        sources = []
        legacy_paths = []
        if pinned is None:
            # 保存済みPHPを持たない旧Runでは、manifestのパスと文字コードで現在の入力を読む。
            project = Path(config["project_root"])
            legacy_paths = [(_inside_file(project, name), name, encoding) for name, encoding in encodings.items()]
        else:
            pins_by_path = {source["path"]: source for source in pinned}
            for name in encodings:
                if name not in pins_by_path:
                    raise ValueError(f"移行対象のPHPがRunに保存されていません: {name}")
                sources.append(pins_by_path[name])
        targets = [path.as_posix() for path in target_paths(unit)]
        right_paths = {}
        draft_files = []
        missing_files = []
        agent_root = run_path / "agent-workspace"
        use_draft = include_draft and record.get("status") in {"junie_running", "editor_followup", "repairing"}
        if use_draft and (agent_root.is_symlink() or not agent_root.resolve().is_relative_to(run_path.resolve())):
            raise ValueError("Junieの作業場所を安全に参照できません。")
        paths = [Path(config["project_root"]) / name for _, name, _ in legacy_paths]
        paths.extend(worktree / name for name in targets)
        if use_draft:
            paths.extend(agent_root / name for name in targets)
        before = [_file_version(path) for path in paths]
        captured_hashes = {}
        for name in targets:
            if use_draft and (agent_root / name).exists():
                right_paths[name] = _inside_file(agent_root.resolve(), name)
                draft_files.append(name)
            elif include_draft and not (worktree / name).exists():
                # まだ生成されていない移行先は空のペインにし、原文を先に確認できる。
                missing_files.append(name)
            else:
                right_paths[name] = _inside_file(worktree, name)
        source_hashes = {}
        left_files = []
        for path, name, encoding in legacy_paths:
            encoding = input_encoding(path, encoding)
            content = path.read_bytes()
            source_hashes[path] = hashlib.sha256(content).hexdigest()
            text = content.decode(encoding).replace("\r\n", "\n").replace("\r", "\n")
            left_files.append((Path(name).as_posix(), text))
        for source in sources:
            text = read_pinned_php(capsule_path, source).decode(source["encoding"])
            left_files.append((Path(source["path"]).as_posix(), text.replace("\r\n", "\n").replace("\r", "\n")))
        right_files = []
        for name in targets:
            if name in missing_files:
                right_files.append((name, ""))
                continue
            path = right_paths[name]
            content = path.read_bytes()
            right_files.append((name, content.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")))
            if name in draft_files:
                source_hashes[path] = hashlib.sha256(content).hexdigest()
            if target_hashes is not None:
                captured_hashes[name] = source_hashes.get(path) or hashlib.sha256(content).hexdigest()
        # worktreeはロック中に直接編集されない。外部PHPとJunieの下書きだけを再読込する。
        contents_changed = any(sha256_file(path) != digest for path, digest in source_hashes.items())
        # 別プロセスによるハーネスのファイル置換や工程変更も検出する。
        after = [_file_version(path) for path in paths]
        _, _, latest, _ = load_saved_run(config, run_id)
        if contents_changed or before != after or any(
            record.get(key) != latest.get(key) for key in ("status", "target_apply_pending")
        ):
            raise ValueError("取得中にソースが変更されました。もう一度お試しください。")
        if target_hashes is not None:
            target_hashes.update(captured_hashes)
    left_text, left_segments = _pane(left_files)
    right_text, right_segments = _pane(right_files)
    payload = {
        "kind": "diff-viewer-workspace",
        "version": 1,
        "snapshot": {
            "runId": run_id, "capturedAt": datetime.now(timezone.utc).isoformat(),
            "draftFiles": draft_files, "missingFiles": missing_files,
        },
        "workspace": {
            "name": unit["id"].strip().encode("utf-16-le")[:50].decode("utf-16-le", errors="ignore"),
            "leftText": left_text, "rightText": right_text,
            "leftSegments": left_segments, "rightSegments": right_segments,
            "leftActiveFile": left_files[0][0], "rightActiveFile": right_files[0][0],
            "anchors": {"manualAnchors": [], "staleManualAnchors": []},
        },
    }
    if isinstance(focus, dict):
        name, line = focus.get("path"), focus.get("line")
        if isinstance(name, str) and type(line) is int:
            for segment in right_segments:
                if Path(segment["fileName"]) == Path(name) and 1 <= line <= segment["lineCount"]:
                    payload["workspace"]["rightActiveFile"] = segment["fileName"]
                    payload["workspace"]["rightCursor"] = {
                        "lineNumber": segment["startLine"] + line - 1, "column": 1,
                    }
                    break
    return run_path, payload


def save_workspace(run_path: Path, payload: dict[str, Any]) -> str:
    """過去の比較と区別できるIDを発行し、ワークスペースを上書きせずに保存する。"""
    snapshot_id = uuid.uuid4().hex
    directory = run_path / "review-snapshots"
    directory.mkdir(exist_ok=True)
    if directory.is_symlink() or not directory.resolve().is_relative_to(run_path):
        raise ValueError("スナップショットの保存先を確認してください。")
    with (directory / f"{snapshot_id}.json").open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False)
    return snapshot_id


def create_snapshot(config: dict[str, Any], run_id: str, *, focus: dict[str, Any] | None = None) -> str:
    return save_workspace(*capture_workspace(config, run_id, focus=focus, include_draft=True))


def snapshot_bytes(config: dict[str, Any], run_id: str, snapshot_id: str) -> bytes:
    """比較IDから当該Runに属する保存済みJSONだけを読み出す。"""
    if re.fullmatch(r"[a-f0-9]{32}", snapshot_id) is None:
        raise ValueError("スナップショットIDが無効です。")
    run_path, _, _, _ = load_saved_run(config, run_id)
    content = _inside_file(run_path, f"review-snapshots/{snapshot_id}.json").read_bytes()
    payload = json.loads(content)
    snapshot = payload.get("snapshot") if isinstance(payload, dict) else None
    if not isinstance(snapshot, dict) or snapshot.get("runId") != run_id:
        raise ValueError("スナップショットのRun IDが指定されたRunと一致しません。")
    return content


def review_html(config: dict[str, Any], workspace: bytes) -> bytes:
    """既存ViewerのJSON取込に比較データと初期表示位置を渡す。"""
    if not (viewer_html := config.get("diff_viewer_html")):
        raise ValueError("設定の差分ビューアーのHTMLを指定してください。")
    # HTMLは設定で明示した場所から読み、アプリ外のViewerも利用できる。
    path = (Path(config["project_root"]) / viewer_html).resolve()
    html = path.read_bytes()
    closing = list(re.finditer(rb"</body\s*>", html, flags=re.IGNORECASE))
    if not closing or b"workspace-import-file" not in html:
        raise ValueError("設定したHTMLにはDiff Viewerのワークスペース取込機能がありません。")
    # ソースの文字列をHTMLとして実行せず、Viewerの通常のJSON取込に渡す。
    payload = workspace.replace(b"<", b"\\u003c")
    # 取込でカーソルが復元されても画面外に残るため、カード生成後にViewerの行ジャンプを使う。
    bootstrap = """
<script id="harness-review-workspace" type="application/json">""" + payload.decode("utf-8") + """</script>
<script>
(() => {
  const message = document.createElement("p");
  const failed = () => {
    message.setAttribute("role", "alert");
    message.textContent = "比較データを自動で読み込めませんでした。ページを再読み込みするか、保存したJSONを取り込んでください。";
    document.body.prepend(message);
  };
  // The bundled Viewer awaits restoration before creating both editors;
  // editor creation and import-handler registration share one synchronous turn.
  const timer = setInterval(() => {
    const input = document.querySelector("#workspace-import-file");
    if (!input || !document.querySelector("#left-editor .monaco-editor")
        || !document.querySelector("#right-editor .monaco-editor")) return;
    clearInterval(timer);
    clearTimeout(timeout);
    message.remove();
    let observer;
    try {
      const payload = document.querySelector("#harness-review-workspace").textContent;
      const {rightCursor, rightActiveFile, rightSegments} = JSON.parse(payload).workspace;
      const segment = rightCursor && rightSegments?.find(item => item.fileName === rightActiveFile);
      const cards = document.querySelector("#right-file-cards");
      const gotoLine = document.querySelector("#right-goto-line-input");
      if (segment && cards && gotoLine) {
        observer = new MutationObserver(() => {
          const card = Array.from(cards.querySelectorAll("button[data-file]"))
            .find(button => button.dataset.file === rightActiveFile);
          if (!card) return;
          observer.disconnect();
          requestAnimationFrame(() => requestAnimationFrame(() => {
            gotoLine.dispatchEvent(new KeyboardEvent("keydown", {
              key: "Escape", bubbles: true, cancelable: true
            }));
            card.click();
            document.activeElement?.dispatchEvent(new KeyboardEvent("keydown", {
              key: "g", code: "KeyG", ctrlKey: true, bubbles: true, cancelable: true
            }));
            if (document.activeElement !== gotoLine) return;
            gotoLine.value = String(rightCursor.lineNumber - segment.startLine + 1);
            gotoLine.dispatchEvent(new KeyboardEvent("keydown", {
              key: "Enter", bubbles: true, cancelable: true
            }));
          }));
        });
        observer.observe(cards, {childList: true});
        window.addEventListener("pagehide", () => observer.disconnect(), {once: true});
      }
      const transfer = new DataTransfer();
      transfer.items.add(new File([payload], "review-workspace.json", {type: "application/json"}));
      input.files = transfer.files;
      input.dispatchEvent(new Event("change", {bubbles: true}));
    } catch (error) {
      observer?.disconnect();
      console.error("Workspace import failed:", error);
      failed();
    }
  }, 50);
  const timeout = setTimeout(() => {
    message.setAttribute("role", "status");
    message.textContent = "ビューアーの起動を待っています。起動後に比較データを自動で読み込みます。";
    document.body.prepend(message);
  }, 15000);
  window.addEventListener("pagehide", () => {
    clearInterval(timer);
    clearTimeout(timeout);
  }, {once: true});
})();
</script>
"""
    offset = closing[-1].start()
    return html[:offset] + bootstrap.encode("utf-8") + html[offset:]
