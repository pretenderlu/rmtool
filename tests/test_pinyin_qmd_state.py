"""Execute the injected IME JavaScript in Qt, with controlled I/O and cursors."""

import os
from pathlib import Path
import re
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt5 import QtCore, QtQml, QtWidgets


SOURCE = Path(__file__).resolve().parents[1] / "pinyin-input/qmd/pinyin-input.qmd"


class PinyinQmdStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.engine = QtQml.QJSEngine()
        self.js(r"""
            var later = [], requests = [], logs = [], clock = 10000;
            var Date = {now: function() { return clock; }};
            var console = {warn: function(message) { logs.push(String(message)); }};
            var Qt = {callLater: function(fn) { later.push(fn); }};
            function flush() {
                var limit = 1000;
                while (later.length && --limit) later.shift()();
                if (!limit) throw Error("unbounded event loop");
            }
            function XMLHttpRequest() { requests.push(this); this.readyState = 0; }
            XMLHttpRequest.prototype.open = function(method, url, async) {
                if (async === false) throw Error("synchronous XHR");
                this.url = url;
            };
            XMLHttpRequest.prototype.send = function() {};
            XMLHttpRequest.prototype.abort = function() { this.aborted = true; };
            function reply(xhr, status, body) {
                xhr.status = status; xhr.responseText = body; xhr.readyState = 4;
                if (xhr.onreadystatechange) xhr.onreadystatechange();
            }
            function queries() {
                return requests.filter(function(r) { return r.url.indexOf('/candidates?') >= 0; });
            }
            function polls() {
                return requests.filter(function(r) { return r.url.indexOf('/pop-all') >= 0; });
            }
            function timer() {
                return {running: false, start: function() { this.running = true; },
                        restart: function() { this.start(); },
                        stop: function() { this.running = false; }};
            }
            var candidatesDebounce = timer(), commitTimeout = timer();
            var ctrlConn = {target: null}, textWatcher = {lastText: ''};
            var doc = 'prefix:', ctrl = {
                textCursorIndex: doc.length,
                beginInputMethodTransaction: function() {},
                commitInputMethod: function() {}, clearComposeRange: function() {},
                endInputMethodTransaction: function() {},
                replaceComposeText: function(text) {
                    doc = doc.slice(0, this.textCursorIndex) + text + doc.slice(this.textCursorIndex);
                },
                setCursorIndex: function(pos) {
                    this.textCursorIndex = pos;
                    if (pinyinIME.cursorChanged) pinyinIME.cursorChanged();
                },
                deleteText: function(before, after) {
                    doc = doc.slice(0, this.textCursorIndex - before) + doc.slice(this.textCursorIndex + after);
                    this.textCursorIndex -= before;
                    if (pinyinIME.cursorChanged) pinyinIME.cursorChanged();
                },
                selectTextRange: function() { throw Error('must not select unrelated text'); }
            };
            var pinyinIME = {};
        """)
        for name, value in re.findall(
            r'^            (?:readonly )?property \w+ (\w+): ("[^"\n]*"|true|false|null|-?\d+|\[\])\s*$',
            source, re.M,
        ):
            self.js(f"pinyinIME.{name} = {value};")
        punct = re.search(r'property var _punctMap: (\(\{.*?\}\))', source, re.S).group(1)
        self.js("pinyinIME._punctMap = " + punct)
        functions = re.findall(r'^            function \w+\(.*?^            \}', source, re.M | re.S)
        names = [re.search(r'function (\w+)', fn).group(1) for fn in functions]
        cursor = re.search(r'^                function onTextCursorIndexChanged\(\).*?^                \}', source, re.M | re.S).group()
        watcher = re.search(r'id: textWatcher.*?onTriggered: \{(.*?)^                \}', source, re.M | re.S).group(1)
        timeout = re.search(r'id: commitTimeout.*?onTriggered: ([^\n]+)', source, re.S)
        self.js("with (pinyinIME) {\n" + "\n".join(functions) + "\n" + cursor
                + "\n" + ";".join(f"pinyinIME.{name} = {name}" for name in names)
                + "; pinyinIME.cursorChanged = onTextCursorIndexChanged;"
                + "pinyinIME.watchText = function() { with(textWatcher) {" + watcher + "}};"
                + "pinyinIME.expireCommit = function() {" + (timeout.group(1) if timeout else "") + "}; }")
        self.js("""
            Object.defineProperty(pinyinIME, 'pageCount', {get: function() {
                return Math.max(1, Math.ceil(this.candidates.length / this.pageSize));
            }});
            pinyinIME.focusTarget = {controller: ctrl};
            pinyinIME.active = pinyinIME.isChineseMode = pinyinIME.useDirectCommit = true;
            pinyinIME.refreshCursorPosition = function() {};
            function chunk(chars) {
                pinyinIME._pollChars();
                reply(polls().slice(-1)[0], 200, chars);
                flush();
            }
        """)

    def js(self, code):
        result = self.engine.evaluate(code)
        self.assertFalse(result.isError(), result.toString())
        return result.toVariant()

    def test_out_of_order_candidate_reply_is_ignored(self):
        self.js("pinyinIME.pinyinBuffer='ni'; pinyinIME._doFetchCandidates(); var old=queries()[0];")
        self.js("pinyinIME.pinyinBuffer='nihao'; pinyinIME._doFetchCandidates(); reply(queries()[1],200,'[\"hello\"]');")
        self.js("reply(old,200,'[\"stale\"]');")
        self.assertEqual(self.js("pinyinIME.candidates"), ["hello"])

    def test_old_reply_cannot_restore_candidates_after_clear_and_retype(self):
        self.js("chunk('ni'); var old=queries().slice(-1)[0]; pinyinIME.clearState(); chunk('ni');")
        self.js("reply(queries().slice(-1)[0],200,'[\"current\"]'); reply(old,200,'[\"stale\"]');")
        self.assertEqual(self.js("pinyinIME.candidates"), ["current"])

    def test_space_queries_current_buffer_and_keeps_next_chunk_order(self):
        self.js("chunk('ni'); reply(queries().slice(-1)[0],200,'[\"stale\"]'); chunk('hao next ');")
        self.assertEqual(self.js("doc.replace(/\\u200b/g,'')"), "prefix:")
        self.js("reply(queries().slice(-1)[0],200,'[\"hello\"]'); flush();")
        self.assertEqual(self.js("doc.replace(/\\u200b/g,'')"), "prefix:hello")
        self.js("reply(queries().slice(-1)[0],200,'[\"next-word\"]'); flush();")
        self.assertEqual(self.js("doc.replace(/\\u200b/g,'')"), "prefix:hellonext-word")

    def test_failed_commit_keeps_raw_case_and_following_punctuation(self):
        self.js("chunk('NiHao!next'); reply(queries().slice(-1)[0],503,''); flush();")
        self.assertEqual(self.js("doc.replace(/\\u200b/g,'')"), "prefix:NiHao\uff01")
        self.assertEqual(self.js("pinyinIME.pinyinBuffer"), "next")

    def test_commit_timeout_keeps_raw_and_ignores_late_reply(self):
        self.js("chunk('NiHao next'); var pending=queries().slice(-1)[0]; pinyinIME.expireCommit(); flush();")
        self.assertEqual(self.js("doc.replace(/\\u200b/g,'')"), "prefix:NiHao")
        self.assertEqual(self.js("pinyinIME.pinyinBuffer"), "next")
        self.js("reply(pending,200,'[\"too-late\"]'); flush();")
        self.assertEqual(self.js("doc.replace(/\\u200b/g,'')"), "prefix:NiHao")
        self.assertEqual(self.js("pinyinIME.pinyinBuffer"), "next")

    def test_digit_and_click_query_current_not_displayed_old_buffer(self):
        for trigger in ("chunk('hao2');", "chunk('hao'); pinyinIME.selectCandidate(1);"):
            with self.subTest(trigger=trigger):
                self.setUp()
                self.js("chunk('ni'); reply(queries().slice(-1)[0],200,'[\"old-first\",\"old-second\"]');")
                self.js(trigger)
                self.js("reply(queries().slice(-1)[0],200,'[\"new-first\",\"new-second\"]'); flush();")
                self.assertEqual(self.js("doc.replace(/\\u200b/g,'')"), "prefix:new-second")

    def test_multiple_punctuation_marks_and_chunks_keep_order(self):
        self.js("chunk('nihao!?'); chunk('next!'); reply(queries().slice(-1)[0],200,'[\"hello\"]'); flush();")
        self.assertEqual(self.js("doc.replace(/\\u200b/g,'')"), "prefix:hello\uff01\uff1f")
        self.js("reply(queries().slice(-1)[0],200,'[\"next-word\"]'); flush();")
        self.assertEqual(self.js("doc.replace(/\\u200b/g,'')"), "prefix:hello\uff01\uff1fnext-word\uff01")

    def test_invalid_service_results_fall_back_to_raw(self):
        for body in ('invalid-json', '{}', '[42]', '[""]', '[]'):
            with self.subTest(body=body):
                self.setUp()
                self.js("chunk('NiHao ');")
                self.js("reply(queries().slice(-1)[0],200," + repr(body) + "); flush();")
                self.assertEqual(self.js("doc.replace(/\\u200b/g,'')"), "prefix:NiHao")

    def test_explicit_backspace_and_enter_keep_direct_input_mode(self):
        self.js("chunk('NiHaox\\b\\rnext');")
        self.assertEqual(self.js("doc.replace(/\\u200b/g,'')"), "prefix:NiHao")
        self.assertEqual(self.js("pinyinIME.pinyinBuffer"), "next")

    def test_digits_only_select_five_visible_candidates(self):
        self.js("chunk('ni'); reply(queries().slice(-1)[0],200,JSON.stringify(['a','b','c','d','e','f','g','h','i','j'])); chunk('6');")
        self.assertEqual(self.js("pinyinIME.pinyinBuffer"), "ni")
        self.assertEqual(self.js("doc.replace(/\\u200b/g,'')"), "prefix:")
        self.js("pinyinIME.pageIdx=1; chunk('2'); flush();")
        self.assertEqual(self.js("doc.replace(/\\u200b/g,'')"), "prefix:g")

    def test_digit_for_missing_visible_entry_keeps_composition(self):
        self.js("chunk('ni5'); reply(queries().slice(-1)[0],200,'[\"only-one\"]'); flush();")
        self.assertEqual(self.js("pinyinIME.pinyinBuffer"), "ni")
        self.assertEqual(self.js("doc.replace(/\\u200b/g,'')"), "prefix:")

    def test_failed_digit_query_commits_raw_not_old_choice(self):
        self.js("chunk('ni'); reply(queries().slice(-1)[0],200,'[\"old\"]'); chunk('hao2'); reply(queries().slice(-1)[0],503,''); flush();")
        self.assertEqual(self.js("doc.replace(/\\u200b/g,'')"), "prefix:nihao")

    def test_cursor_move_does_not_delete_text_or_truncate_composition(self):
        for pos in (2, 0, 10):
            with self.subTest(pos=pos):
                self.setUp()
                self.js("chunk('nihao'); doc += 'suffix'; ctrl.textCursorIndex=" + str(pos) + "; pinyinIME.cursorChanged(); flush();")
                result = self.js("doc.replace(/\\u200b/g,'')")
                self.assertEqual(result, "prefix:suffix")
                self.assertEqual(self.js("pinyinIME.pinyinBuffer"), "nihao")

    def test_cursor_move_cancels_pending_query_without_replacing_selection(self):
        self.js("""
            chunk('nihao!'); var pending=queries().slice(-1)[0];
            ctrl.textCursorIndex=2;
            ctrl.replaceComposeText=function() { throw Error('must not replace a selection on cursor movement'); };
            pinyinIME.cursorChanged(); reply(pending,200,'["hello"]'); flush();
        """)
        self.assertEqual(self.js("doc.replace(/\\u200b/g,'')"), "prefix:")
        self.assertEqual(self.js("pinyinIME.pinyinBuffer"), "nihao")
        self.assertEqual(self.js("pinyinIME._charQueue"), "\uff01")
        self.assertEqual(self.js("logs"), [])

    def test_failing_poll_does_not_spin(self):
        self.js("pinyinIME._pollChars(); reply(polls()[0],503,''); flush();")
        self.assertEqual(self.js("polls().length"), 1)
        self.js("pinyinIME._pollChars();")
        self.assertEqual(self.js("polls().length"), 1)

    def test_poll_backoff_is_capped_and_success_resets_it(self):
        self.js("""
            for (var i=0; i<10; i++) {
                pinyinIME._pollChars();
                reply(polls().slice(-1)[0],503,''); flush();
                if (pinyinIME._charRetryAt-clock > 4000) throw Error('unbounded backoff');
                clock=pinyinIME._charRetryAt;
            }
            pinyinIME._pollChars(); reply(polls().slice(-1)[0],200,''); flush();
        """)
        self.assertEqual(self.js("pinyinIME._charRetryAt"), 0)
        self.assertEqual(self.js("pinyinIME._charRetryDelay"), 250)

    def test_text_mode_commit_does_not_use_stale_candidates(self):
        self.js("""
            pinyinIME.useDirectCommit=false;
            pinyinIME.focusTarget={text:'NiHao!', cursorPosition:6};
            pinyinIME.pinyinBuffer='NiHao'; pinyinIME.candidates=['stale'];
            textWatcher.lastText='NiHao!'; pinyinIME.commitTextMode(1);
        """)
        self.assertEqual(self.js("pinyinIME.focusTarget.text"), "NiHao!")
        self.js("reply(queries().slice(-1)[0],200,'[\"hello\"]'); flush();")
        self.assertEqual(self.js("pinyinIME.focusTarget.text"), "hello!")

    def test_text_mode_preserves_native_input_received_while_query_pending(self):
        self.js("""
            pinyinIME.useDirectCommit=false;
            pinyinIME.focusTarget={text:'NiHao!', cursorPosition:6};
            pinyinIME.pinyinBuffer='NiHao'; textWatcher.lastText='NiHao!';
            pinyinIME.commitTextMode(1);
            pinyinIME.focusTarget.text += 'next'; pinyinIME.focusTarget.cursorPosition=10;
            pinyinIME.watchText();
            reply(queries().slice(-1)[0],200,'["hello"]'); flush(); pinyinIME.watchText();
        """)
        self.assertEqual(self.js("pinyinIME.focusTarget.text"), "hello!next")
        self.assertEqual(self.js("pinyinIME.pinyinBuffer"), "next")

    def test_text_mode_failure_and_native_edit_never_remove_raw_letters(self):
        self.js("""
            pinyinIME.useDirectCommit=false;
            pinyinIME.focusTarget={text:'NiHao!', cursorPosition:6};
            pinyinIME.pinyinBuffer='NiHao'; pinyinIME.commitTextMode(1);
            reply(queries().slice(-1)[0],503,''); flush();
        """)
        self.assertEqual(self.js("pinyinIME.focusTarget.text"), "NiHao!")
        self.js("""
            pinyinIME.pinyinBuffer='NiHao'; pinyinIME.commitTextMode(1);
            pinyinIME.focusTarget.text='unrelated NiHao!';
            pinyinIME.focusTarget.cursorPosition=0;
            reply(queries().slice(-1)[0],200,'["hello"]'); flush();
        """)
        self.assertEqual(self.js("pinyinIME.focusTarget.text"), "unrelated NiHao!")

    def test_focus_change_invalidates_pending_commit(self):
        self.js("chunk('nihao '); var old=queries().slice(-1)[0]; pinyinIME.focusTarget={controller:ctrl}; pinyinIME.clearState(); reply(old,200,'[\"hello\"]'); flush();")
        self.assertEqual(self.js("doc.replace(/\\u200b/g,'')"), "prefix:")
        self.assertEqual(self.js("pinyinIME.pinyinBuffer"), "")

    def test_logs_never_include_committed_or_exception_text(self):
        self.js("pinyinIME.pinyinBuffer='private-letters'; pinyinIME.commitBufferRaw(ctrl); flush();")
        self.js("ctrl.replaceComposeText=function(text) { throw Error(text); }; pinyinIME.insertToDoc(ctrl,'private-error');")
        self.js("pinyinIME.pinyinBuffer='private-commit'; pinyinIME.commitBufferRaw(ctrl);")
        self.assertNotIn("private", " ".join(self.js("logs")))

    def test_keyboard_and_focus_diagnostics_do_not_dump_user_text(self):
        self.js("""
            var item={text:'private-text', label:'private-label', children:[],
                toString:function() { return 'private-object'; }};
            pinyinIME.Window={window:{activeFocusItem:item}};
            pinyinIME.nudgeToolbar=function() {};
            pinyinIME.activateForCurrentFocus();
            if (pinyinIME.dumpKbdDeep) pinyinIME.dumpKbdDeep(item,0);
            if (pinyinIME.dumpTree) pinyinIME.dumpTree(item,0);
            flush();
        """)
        self.assertNotIn("private", " ".join(self.js("logs")))

    def test_injected_qml_compiles_with_real_qt(self):
        source = SOURCE.read_text(encoding="utf-8")
        injected = source.split('        INSERT {\n', 1)[1].rsplit('        }\n    END TRAVERSE', 1)[0]
        engine = QtQml.QQmlEngine()
        component = QtQml.QQmlComponent(engine)
        component.setData(('import QtQuick 2.15\nimport QtQuick.Window 2.15\n' + injected).encode(), QtCore.QUrl())
        self.assertTrue(component.isReady(), '\n'.join(error.toString() for error in component.errors()))


if __name__ == "__main__":
    unittest.main()
