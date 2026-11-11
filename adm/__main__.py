"""Entry point.

  uv run adm            launch the GUI
  uv run adm --selftest parse/scan everything headless + edit round-trip on a copy
  uv run adm --repair   fix claude transcripts with dangling parentUuid/leafUuid
  uv run adm --smoke    launch GUI and auto-close after 4s
  uv run adm --transplant <id|file> --to claude|codex|opencode|json
                        [--cwd DIR] [--title T] [--out DIR]
                        [--no-tools] [--reasoning] [--sidechain]
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _posix(path) -> str:
    """Windows path -> POSIX path for the shell scripts used in tests."""
    cyg = shutil.which("cygpath")
    if cyg:
        try:
            return subprocess.check_output(
                [cyg, "-u", str(path)], text=True,
                encoding="utf-8", errors="replace").strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return str(path).replace("\\", "/")


def repair_chains() -> int:
    """Repair claude transcripts whose parentUuid / last-prompt.leafUuid point
    at deleted records (sessions the CLI can no longer render). Every fixed
    file is backed up first; running sessions are skipped."""
    from adm import claude_store
    from adm import config as cfg
    from adm.jsonlio import read_lines

    running = claude_store.running_session_ids()
    fixed = skipped = clean = 0
    files = sorted(cfg.CLAUDE_PROJECTS.rglob("*.jsonl")) \
        if cfg.CLAUDE_PROJECTS.is_dir() else []
    for f in files:
        if f.stem in running:
            skipped += 1
            print(f"SKIP (running) {f}")
            continue
        try:
            pre = claude_store.dangling_counts(read_lines(f))
        except OSError:
            continue
        if pre == (0, 0):
            clean += 1
            continue
        n = claude_store.repair_file_chain(f)
        post = claude_store.dangling_counts(read_lines(f))
        print(f"FIXED {f.relative_to(cfg.CLAUDE_PROJECTS)}: "
              f"parent={pre[0]} leaf={pre[1]} -> {post} ({n} lines)")
        fixed += 1
    print(f"checked {len(files)} files: {fixed} repaired, {skipped} skipped "
          f"(running), {clean} clean")
    return 0


def selftest() -> int:
    from adm import claude_store, codex_store, local_store
    from adm.jsonlio import SessionEditor

    print("== scanning claude ==")
    cl = claude_store.scan_claude(lambda s: None)
    print(f"claude sessions: {len(cl)}")
    for m in cl[:3]:
        print(f"  [{m.id[:8]}] {m.display_title[:50]!r} proj={m.project} "
              f"msgs={m.n_messages} size={m.size} running={m.running}")

    print("== scanning codex ==")
    cx = codex_store.scan_codex(lambda s: None)
    print(f"codex sessions: {len(cx)}")
    for m in cx[:3]:
        print(f"  [{m.id[:8]}] {m.display_title[:50]!r} proj={m.project} "
              f"turns={m.n_messages} tokens={m.tokens} arch={m.archived}")

    ok = True
    for label, metas, loader in [
        ("claude", cl, claude_store.load_claude),
        ("codex", cx, codex_store.load_codex),
    ]:
        # pick a small session for a full parse check
        small = sorted(metas, key=lambda m: m.size)[:2]
        for m in small:
            sess = loader(m)
            print(f"== load {label} {m.id[:8]}: {sess.total_lines} lines, "
                  f"{len(sess.messages)} messages ==", flush=True)
            for msg in sess.messages[:3]:
                print(f"   {msg.role:10s} L{msg.line:<5d} "
                      f"{msg.plain_text(90)!r}")
            if not sess.messages and m.n_messages:
                print(f"   !! expected messages but parsed 0")
                ok = False

    # edit round-trip on a temp copy
    print("== edit round-trip ==")
    from adm import config as _cfg
    orig_backup = _cfg.BACKUP_DIR
    tmpdir = Path(tempfile.mkdtemp())
    _cfg.BACKUP_DIR = tmpdir / "backups"
    if cl:
        m = sorted(cl, key=lambda x: x.size)[0]
        tmp = tmpdir / "copy.jsonl"
        shutil.copy(m.path, tmp)
        ed = SessionEditor(tmp)
        ed.note_snapshot("claude", "test")
        claude_store.append_title(ed, "test", "selftest title")
        ed.save()
        ed2 = SessionEditor(tmp)
        ed2.note_snapshot("claude", "test")
        claude_store.append_user_message(ed2, "test", "C:\\tmp", "hello from selftest")
        ed2.save()
        txt = tmp.read_text(encoding="utf-8")
        assert "selftest title" in txt and "hello from selftest" in txt
        print("   claude backup + append + save + re-edit: OK")
    if cx:
        m = sorted(cx, key=lambda x: x.size)[0]
        tmp = tmpdir / "roll.jsonl"
        shutil.copy(m.path, tmp)
        from adm.models import SessionMeta
        fake = SessionMeta(source="codex", id="test", path=str(tmp),
                           project="t")
        sess = codex_store.load_codex(fake)
        ed = SessionEditor(tmp)
        ed.note_snapshot("codex", "test")
        codex_store.append_user_message(ed, "hello from codex selftest")
        if sess.messages:
            msg = sess.messages[0]
            codex_store.apply_text_edits(ed, msg, {0: "EDITED TEXT"})
        ed.save()
        txt = tmp.read_text(encoding="utf-8")
        assert "hello from codex selftest" in txt
        assert "EDITED TEXT" in txt
        print(f"   codex load+edit+append: OK ({sess.total_lines} lines)")

    # delete + chain repair: a raw line delete must dangle refs, and both
    # repair paths must leave the parentUuid/leafUuid graph consistent
    print("== delete + chain repair ==")
    from adm.jsonlio import read_lines
    from adm.models import SessionMeta
    cands = [m for m in cl if m.n_messages >= 6 and not m.subagent]
    if cands:
        m = sorted(cands, key=lambda x: x.size)[0]
        victim_src = None
        tmpf = tmpdir / "chain.jsonl"
        shutil.copy(m.path, tmpf)
        fake = SessionMeta(source="claude", id="chain-test", path=str(tmpf),
                           project="t")
        sess = claude_store.load_claude(fake)
        mids = [x for x in sess.messages[1:-1]
                if len(claude_store.linked_lines(sess, x)) == 1]
        if mids:
            victim_src = mids[len(mids) // 2]

            # (1) standalone repair on an already-broken file
            ed = SessionEditor(tmpf)
            ed.note_snapshot("claude", "chain-test")
            linked = claude_store.linked_lines(sess, victim_src)
            ed.delete_lines(linked)
            ed.save()  # deliberately save the broken state
            pre = claude_store.dangling_counts(read_lines(tmpf))
            assert pre != (0, 0), "expected dangling refs after raw delete"
            n = claude_store.repair_file_chain(tmpf)
            post = claude_store.dangling_counts(read_lines(tmpf))
            assert post == (0, 0), f"repair_file_chain left {post}"
            print(f"   raw delete dangles {pre} -> repair_file_chain "
                  f"({n} lines) -> clean: OK")

            # (2) in-editor relink at delete time (the production path)
            shutil.copy(m.path, tmpf)
            sess = claude_store.load_claude(fake)
            ed = SessionEditor(tmpf)
            ed.note_snapshot("claude", "chain-test")
            linked = claude_store.linked_lines(sess, victim_src)
            ed.delete_lines(linked)
            claude_store.repair_after_delete(ed, linked)
            ed.save()
            post = claude_store.dangling_counts(read_lines(tmpf))
            assert post == (0, 0), f"repair_after_delete left {post}"
            print("   delete + repair_after_delete -> clean: OK")
        else:
            print("   (no unpaired middle message found; skipped)")
    else:
        print("   (no session with >=6 messages; skipped)")
    # consciousness upload: bundle -> install.py on a temp "other machine"
    print("== consciousness upload round-trip ==")
    import json
    import sqlite3
    import subprocess
    from adm import transfer
    up_root = tmpdir / "upload"
    other_home = tmpdir / "other-machine"
    other_home.mkdir()
    upload_cands = [x for x in cl if not x.subagent and x.n_messages > 0]
    if upload_cands:
        m = sorted(upload_cands, key=lambda x: x.size)[0]
        b = transfer.export_session(m, up_root)
        md = json.loads((b / "metadata.json").read_text(encoding="utf-8"))
        work = other_home / "work-claude"
        chome = other_home / "claude-home"
        rc = subprocess.call(
            [sys.executable, str(b / "install.py"), "--cwd", str(work),
             "--claude-home", str(chome)], stdout=subprocess.DEVNULL)
        assert rc == 0, "install.py failed for claude"
        installed = (chome / "projects" / transfer.claude_project_dir_name(str(work))
                     / f"{md['new_id']}.jsonl")
        assert installed.exists(), "claude transcript not installed"
        recs = [json.loads(x) for x in
                installed.read_text(encoding="utf-8").splitlines() if x.strip()]
        assert all(r.get("sessionId") in (None, md["new_id"]) for r in recs), \
            "sessionId not rewritten"
        cwds = [r.get("cwd") for r in recs if r.get("cwd")]
        assert cwds and all(c == str(work) for c in cwds), "cwd not rewritten"
        from adm.models import SessionMeta
        fake = SessionMeta(source="claude", id=md["new_id"], path=str(installed),
                           project="t")
        assert claude_store.load_claude(fake).messages, "installed session unreadable"
        print(f"   claude bundle {b.name}: install.py -> transcript parsed: OK")

        # platform scripts: PowerShell installer end-to-end, sh syntax check
        for name in ("install.bat", "install.ps1", "install.sh", "install.py"):
            assert (b / name).exists(), f"bundle missing {name}"
        ps = shutil.which("pwsh") or shutil.which("powershell")
        if ps:
            work_ps = other_home / "work-claude-ps1"
            work_ps.mkdir()
            # GetFullPath may expand 8.3 short names in TMP, so compare the
            # long form (which is also what Node's process.cwd() returns).
            work_ps = work_ps.resolve()
            chome_ps = other_home / "claude-home-ps1"
            rc = subprocess.call(
                [ps, "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(b / "install.ps1"), "-Cwd", str(work_ps),
                 "-ClaudeHome", str(chome_ps)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            assert rc == 0, f"install.ps1 failed for claude (rc={rc})"
            inst_ps = (chome_ps / "projects"
                       / transfer.claude_project_dir_name(str(work_ps))
                       / f"{md['new_id']}.jsonl")
            assert inst_ps.exists(), "install.ps1 did not write the transcript"
            recs_ps = [json.loads(x) for x in
                       inst_ps.read_text(encoding="utf-8").splitlines() if x.strip()]
            assert all(r.get("sessionId") in (None, md["new_id"]) for r in recs_ps), \
                "install.ps1: sessionId not rewritten"
            cwds_ps = [r.get("cwd") for r in recs_ps if r.get("cwd")]
            assert cwds_ps and all(c == str(work_ps) for c in cwds_ps), \
                "install.ps1: cwd not rewritten"
            print(f"   claude bundle {b.name}: install.ps1 -> transcript parsed: OK")
        sh = shutil.which("sh")
        if sh:
            rc = subprocess.call([sh, "-n", str(b / "install.sh")],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            assert rc == 0, "install.sh has a syntax error"
            print(f"   claude bundle {b.name}: install.sh syntax: OK")

    if cx:
        from adm import config as _cfg2
        codex_home = other_home / "codex-home"
        codex_home.mkdir()
        shutil.copy(_cfg2.CODEX_STATE_DB, codex_home / "state_5.sqlite")
        m = sorted(cx, key=lambda x: x.size)[0]
        b = transfer.export_session(m, up_root)
        md = json.loads((b / "metadata.json").read_text(encoding="utf-8"))
        work = other_home / "work-codex"
        rc = subprocess.call(
            [sys.executable, str(b / "install.py"), "--cwd", str(work),
             "--codex-home", str(codex_home)], stdout=subprocess.DEVNULL)
        assert rc == 0, "install.py failed for codex"
        rolls = list((codex_home / "sessions").rglob(f"rollout-*-{md['new_id']}.jsonl"))
        assert rolls, "codex rollout not installed"
        import sqlite3
        con = sqlite3.connect(codex_home / "state_5.sqlite")
        try:
            row = con.execute("SELECT rollout_path, cwd FROM threads WHERE id=?",
                              (md["new_id"],)).fetchone()
        finally:
            con.close()
        assert row and str(rolls[0]) == row[0] and row[1] == str(work), \
            "codex thread not registered"
        print(f"   codex bundle {b.name}: install.py -> rollout + sqlite row: OK")

    # cross-tool transplant: neutral builder + Claude/JSON writers (no real stores)
    print("== cross-tool transplant ==")
    from adm import transplant
    src = None
    src_msgs = None
    for cand in sorted([x for x in cl + cx if not x.subagent and 2 <= x.n_messages],
                       key=lambda x: x.size):
        if cand.size > 2_000_000:
            continue
        try:
            neutral = transplant.build_transcript(
                transplant.load_source(cand).messages)
        except Exception:  # noqa: BLE001
            continue
        if sum(1 for x in neutral if x.role == "user") and \
                sum(1 for x in neutral if x.role == "assistant"):
            src, src_msgs = cand, neutral
            break
    if src and src_msgs:
        prev_claude_home = os.environ.get("CLAUDE_CONFIG_DIR")
        tp_home = tmpdir / "transplant-claude-home"
        os.environ["CLAUDE_CONFIG_DIR"] = str(tp_home)
        try:
            work = tmpdir / "transplant-work"
            res = transplant.write_transcript(
                src_msgs, "transplant selftest", str(work), "claude",
                source=src.source, source_name=src.source_badge,
                model=src.model or "")
            tp_file = Path(res.path)
            assert tp_file.is_file(), "transplant claude file missing"
            assert tp_file.parent.parent.name == "projects", "project dir layout"
            fake_tp = SessionMeta(source="claude", id=res.session_id,
                                  path=str(tp_file), project="t", cwd=res.cwd)
            tp_loaded = claude_store.load_claude(fake_tp)
            assert tp_loaded.messages, "transplanted claude session unreadable"
            assert claude_store.dangling_counts(tp_file.read_text(
                encoding="utf-8").splitlines()) == (0, 0), "transplant chain broken"
            print(f"   {src.source} -> claude ({len(src_msgs)} msgs): parse + chain: OK")

            gen = transplant.write_transcript(
                src_msgs, "transplant selftest", str(work), "json",
                source=src.source, source_name=src.source_badge,
                out_dir=tmpdir / "transplant-json")
            gj = json.loads((Path(gen.path) / "transcript.json").read_text(
                encoding="utf-8"))
            assert gj["format"] == "ai-dialog-transcript" and gj["messages"], \
                "generic transcript invalid"
            assert (Path(gen.path) / "transcript.md").is_file(), "markdown missing"
            print(f"   {src.source} -> generic json+md: OK")

            # cross-tool conversion carried by the bundle: the same source
            # session pre-rendered as another tool's transcript
            conv_bundle = transfer.export_session(src, tmpdir / "convert-bundle")
            conv_md = json.loads((conv_bundle / "metadata.json").read_text(
                encoding="utf-8"))
            tp = conv_md.get("transplant")
            if tp:
                for name in ("convert.bat", "convert.ps1", "convert.sh",
                             "convert.py", "transplant/claude.jsonl",
                             "transplant/codex.jsonl", "transplant/opencode.json",
                             "transplant/neutral/transcript.json"):
                    assert (conv_bundle / name).exists(), f"bundle missing {name}"
                conv_work = (tmpdir / "converted codex").resolve()
                conv_work.mkdir()
                conv_home = tmpdir / "codex-home-conv"
                conv_home.mkdir()
                shutil.copy(_cfg.CODEX_STATE_DB, conv_home / "state_5.sqlite")
                rc = subprocess.call(
                    [sys.executable, str(conv_bundle / "convert.py"), "codex",
                     "--cwd", str(conv_work), "--codex-home", str(conv_home)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                assert rc == 0, "convert.py codex failed"
                conv_rolls = list(conv_home.rglob(
                    f"rollout-*-{tp['codex']['id']}.jsonl"))
                assert conv_rolls, "convert.py did not write a rollout"
                conv_text = conv_rolls[0].read_text(encoding="utf-8")
                assert "__ADM_TARGET_CWD__" not in conv_text, "placeholder left"
                conv_recs = [json.loads(x) for x in conv_text.splitlines()
                             if x.strip()]
                assert conv_recs[0]["payload"]["cwd"] == str(conv_work), \
                    "converted cwd not rewritten"
                conv_db = sqlite3.connect(conv_home / "state_5.sqlite")
                try:
                    conv_row = conv_db.execute(
                        "SELECT rollout_path, cwd FROM threads WHERE id=?",
                        (tp["codex"]["id"],)).fetchone()
                finally:
                    conv_db.close()
                assert conv_row and conv_row[1] == str(conv_work), \
                    "converted thread not registered"
                print(f"   {src.source} -> codex ({tp['n_messages']} msgs): "
                      f"convert.py into a new folder: OK")

                # the shell converter must work without Python at all (claude)
                sh = shutil.which("sh")
                if sh:
                    rc = subprocess.call([sh, "-n", str(conv_bundle / "convert.sh")],
                                         stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
                    assert rc == 0, "convert.sh has a syntax error"
                    conv_claude_home = tmpdir / "claude-home-conv"
                    sh_work = tmpdir / "converted claude"
                    sh_work.mkdir()
                    env = dict(os.environ)
                    env["CLAUDE_CONFIG_DIR"] = _posix(conv_claude_home)
                    posix_work = _posix(sh_work)
                    rc = subprocess.call(
                        [sh, _posix(conv_bundle) + "/convert.sh",
                         "claude", posix_work],
                        env=env, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL)
                    assert rc == 0, "convert.sh claude failed"
                    sh_files = list(conv_claude_home.rglob("*.jsonl"))
                    assert sh_files, "convert.sh did not write a transcript"
                    resolved = subprocess.check_output(
                        [sh, "-c", 'cd "$1" && pwd -P', "_", posix_work],
                        text=True, encoding="utf-8", errors="replace").strip()
                    sh_recs = [json.loads(x) for x in
                               sh_files[0].read_text(encoding="utf-8").splitlines()
                               if x.strip()]
                    assert all(r.get("sessionId") in (None, tp["claude"]["id"])
                               for r in sh_recs), "convert.sh: sessionId not set"
                    assert all(r.get("cwd") in (None, resolved) for r in sh_recs), \
                        "convert.sh: cwd not rewritten"
                    assert claude_store.dangling_counts(
                        sh_files[0].read_text(encoding="utf-8").splitlines()) \
                        == (0, 0), "convert.sh: claude chain broken"
                    print(f"   {src.source} -> claude: convert.sh (no Python): OK")
        finally:
            if prev_claude_home is None:
                os.environ.pop("CLAUDE_CONFIG_DIR", None)
            else:
                os.environ["CLAUDE_CONFIG_DIR"] = prev_claude_home

    _cfg.BACKUP_DIR = orig_backup
    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def transplant_cli(argv: list[str]) -> int:
    """Headless cross-tool transplant, e.g.

    uv run adm --transplant 4f2a1c --to codex --cwd D:/hello
    uv run adm --transplant session.jsonl --to opencode --cwd D:/hello
    uv run adm --transplant opencode-export.json --to claude --cwd D:/hello

    SOURCE is a session id/prefix, a Claude/Codex .jsonl transcript, an
    OpenCode export .json, or a portable transcript.json.
    """
    import argparse
    import json
    import os
    from pathlib import Path

    from adm import transplant

    ap = argparse.ArgumentParser(prog="adm --transplant")
    ap.add_argument("source", help="session id / .jsonl / opencode-export.json")
    ap.add_argument("--to", required=True, choices=list(transplant.TARGETS))
    ap.add_argument("--cwd", default="", help="target working directory")
    ap.add_argument("--title", default="")
    ap.add_argument("--out", default="", help="output root for --to json")
    ap.add_argument("--no-tools", action="store_true",
                    help="drop tool call/result text")
    ap.add_argument("--reasoning", action="store_true",
                    help="include thinking/reasoning text")
    ap.add_argument("--sidechain", action="store_true",
                    help="include Claude subagent records")
    ns = ap.parse_args(argv)

    opt = transplant.TransplantOptions(
        include_tools=not ns.no_tools,
        include_reasoning=ns.reasoning,
        include_sidechain=ns.sidechain,
    )
    src = Path(ns.source).expanduser()
    try:
        if src.is_file() and src.suffix.lower() == ".json":
            raw = src.read_bytes()
            # PowerShell 5.1 `>` / Out-File writes UTF-16; accept both.
            text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") \
                else raw.decode("utf-8-sig")
            data = json.loads(text)
            if data.get("format") == "ai-dialog-transcript":
                title, src_cwd, msgs = transplant.transcript_from_generic(data)
                source, source_name = "json", "portable transcript"
            else:
                title, src_cwd, msgs = transplant.transcript_from_opencode_export(
                    data, opt)
                source, source_name = "opencode", "OpenCode"
            res = transplant.write_transcript(
                msgs, ns.title or title, ns.cwd or src_cwd or os.getcwd(),
                ns.to, source=source, source_name=source_name,
                out_dir=ns.out or None, options=opt)
        else:
            meta = transplant.find_session(ns.source) if not src.is_file() \
                else transplant._meta_from_file(src)
            res = transplant.transplant(
                meta, ns.to, ns.cwd or meta.cwd or None,
                title=ns.title or None, options=opt,
                out_dir=ns.out or None)
    except transplant.TransplantError as e:
        print(f"transplant failed: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"transplant failed: {e}", file=sys.stderr)
        return 1

    print(f"OK  {res.target}  {res.n_messages} messages")
    print(f"  id:       {res.session_id}")
    print(f"  title:    {res.title}")
    print(f"  cwd:      {res.cwd}")
    print(f"  path:     {res.path}")
    for w in res.warnings:
        print(f"  warning:  {w}")
    print(f"  resume:   {res.resume_hint()}")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if "--selftest" in args:
        return selftest()
    if "--repair" in args:
        return repair_chains()
    if "--transplant" in args:
        return transplant_cli([a for a in args if a != "--transplant"])
    try:
        from PyQt6.QtCore import QSettings, QTimer
        from PyQt6.QtWidgets import QApplication
    except ImportError as e:
        print(f"PyQt6 missing: {e}\nrun: uv sync", file=sys.stderr)
        return 2
    from adm import config, i18n
    from adm.ui import theme
    from adm.ui.main_window import MainWindow

    config.ensure_app_dirs()
    i18n.set_lang(str(QSettings("ai-dialog-manager", "adm").value("lang", "zh")))
    app = QApplication(sys.argv)
    app.setStyleSheet(theme.QSS)
    app.setApplicationName("AI Dialog Manager")
    win = MainWindow()
    win.show()
    if "--smoke" in args and os.environ.get("ADM_AUTOTEST"):
        _run_autotest(app, win)
    elif "--smoke" in args:
        QTimer.singleShot(4000, app.quit)
    return app.exec()


def _run_autotest(app, win) -> None:
    """Drive the real UI: wait for scan -> load a session -> render chat ->
    build an edit dialog -> jump. Prints AUTOTEST PASS/FAIL and quits."""
    from PyQt6.QtCore import QTimer
    state = {"ok": True, "stage": "scan"}

    def fail(msg):
        state["ok"] = False
        print(f"AUTOTEST FAIL at {state['stage']}: {msg}", flush=True)
        app.quit()

    def wait_scan():
        if win.scan_thread and win.scan_thread.isRunning():
            QTimer.singleShot(1500, wait_scan)
            return
        if not win.metas:
            fail("no sessions scanned")
            return
        print(f"scan ok: {len(win.metas)} sessions", flush=True)
        # pick a mid-size claude + codex session
        cand = sorted([m for m in win.metas if not m.subagent and m.size > 5000],
                      key=lambda m: m.size)
        picks = [cand[0]] + [m for m in cand if m.source == "codex"][:1]
        state["queue"] = picks
        state["stage"] = "load"
        load_next()

    def load_next():
        if not state.get("queue"):
            print("AUTOTEST PASS", flush=True)
            app.quit()
            return
        m = state["queue"].pop(0)
        state["stage"] = f"load {m.source} {m.id[:8]}"
        win._load_selected(meta=m)
        QTimer.singleShot(2500, lambda: check_loaded(m))

    def check_loaded(m):
        if not win.loaded:
            # big sessions may still be parsing — give it more time
            if win.load_thread and win.load_thread.isRunning():
                QTimer.singleShot(3000, lambda: check_loaded(m))
                return
            fail(f"session {m.id[:8]} failed to load")
            return
        sess = win.loaded
        n = len(sess.messages)
        print(f"loaded {m.source} {m.id[:8]}: {sess.total_lines} lines, {n} msgs",
              flush=True)
        if m.source == "claude":
            try:
                _synthetic_ui_check(win)
            except AssertionError as e:
                fail(f"synthetic ui: {e}")
                return
            except Exception as e:  # noqa: BLE001
                fail(f"synthetic ui: {e}")
                return
        state["stage"] = "render"
        texts = [x for x in sess.messages if x.text_parts]
        if texts:
            try:
                from adm.ui.dialogs import MessageEditDialog
                dlg = MessageEditDialog(texts[0], win)
                dlg.new_texts()
                dlg.deleteLater()
            except Exception as e:  # noqa: BLE001
                fail(f"edit dialog: {e}")
                return
            win.chat.queue_jump(texts[0].line)
        load_next()

    QTimer.singleShot(1000, wait_scan)


def _synthetic_ui_check(win) -> None:
    """Drive the chat view on a synthetic 400-message session (no disk writes):
    pagination, jump auto-expansion and the batch-selection toolbar."""
    from adm.models import LoadedSession, Message, Part, SessionMeta
    from adm.ui.chat_view import PAGE

    meta = SessionMeta(source="claude", id="synthetic", path="synthetic.jsonl",
                       project="t")
    msgs = [Message(line=i, role="user" if i % 2 else "assistant", ts=None,
                    parts=[Part(kind="text", text=f"msg {i}")])
            for i in range(400)]
    synth = LoadedSession(meta=meta, messages=msgs, raw_lines=[],
                          total_lines=400)
    cv = win.chat
    real = cv.session
    cv.set_session(synth)
    assert cv._rendered == PAGE, f"expected first page {PAGE}, got {cv._rendered}"
    # a jump to a deep message must auto-expand pages instead of forcing the
    # user to click "load more" repeatedly
    cv.queue_jump(350)
    assert 350 in cv._frames, "jump did not auto-expand pagination"
    assert cv._rendered >= 351, f"pages not expanded: {cv._rendered}"
    # batch selection toolbar
    cv.batch_btn.setChecked(True)
    assert cv.batch_bar.isVisible() and cv._frames[0].check is not None, \
        "batch mode did not render checkboxes"
    cv._select_visible()
    assert len(cv._selected_msgs()) == 400, "select-visible count wrong"
    from PyQt6.QtWidgets import QApplication
    cv._copy_selection()
    clipped = QApplication.clipboard().text()
    assert "msg 0" in clipped and "msg 399" in clipped, "copy missed messages"
    cv._clear_selection()
    assert not cv._selected_msgs(), "clear selection failed"
    cv.batch_btn.setChecked(False)
    assert not cv.batch_bar.isVisible() and cv._frames[0].check is None, \
        "leaving batch mode kept checkboxes"
    # reverse ("newest first") reading order + jump while reversed
    cv.set_reverse(True)
    assert cv._visible[0].line == 399 and cv._rendered == PAGE, \
        "reverse order did not start with the newest message"
    cv.queue_jump(10)
    assert 10 in cv._frames, "jump failed in reverse order"
    cv.set_reverse(False)
    assert cv._visible[0].line == 0, "forward order not restored"
    # anchor-index reload path used after deletions (read-only reload)
    cv.set_session(real)
    assert cv._rendered <= PAGE
    print("synthetic ui: pagination + jump auto-expand + batch select + "
          "reverse order OK", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
