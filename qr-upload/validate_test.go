package main

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"log"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestEPUBNestedResourcePunctuation(t *testing.T) {
	entries := validEPUB()
	entries["EPUB/Fonts/*_|_:_.ttf"] = "synthetic font fixture"
	entries["EPUB/Text/a:b.xhtml"] = "<html/>"
	data := epub(entries)
	s, calls := setup(t)
	w := httptest.NewRecorder()
	s.ServeHTTP(w, request(s, "book.epub", bytes.NewReader(data)))
	if w.Code != 200 || calls.Load() != 1 {
		t.Fatalf("nested POSIX resource names were refused: %s", w.Body.String())
	}
	checkClean(t, s)
}

func TestEPUBEncryptionAndSymlinksStayBlocked(t *testing.T) {
	for _, test := range []struct {
		name, reason, hint string
		edit               func([]byte)
	}{
		{"encrypted", "encrypted EPUB entry", "ZIP 加密", func(header []byte) {
			binary.LittleEndian.PutUint16(header[8:], binary.LittleEndian.Uint16(header[8:])|1)
		}},
		{"symlink", "unsafe EPUB entry", "符号链接", func(header []byte) {
			header[5] = 3 // Unix creator, so the archive mode identifies the symlink.
			binary.LittleEndian.PutUint32(header[38:], 0120777<<16)
		}},
	} {
		t.Run(test.name, func(t *testing.T) {
			data := epub(validEPUB())
			entry := bytes.Index(data, []byte{'P', 'K', 1, 2})
			test.edit(data[entry:])
			assertValidationDiagnostic(t, "private-title.epub", data, test.reason, test.hint)
		})
	}
}

func TestValidationDiagnostics(t *testing.T) {
	for _, test := range []struct {
		name, reason, hint string
		edit               func(map[string]string)
	}{
		{"missing metadata", "missing EPUB metadata", "缺少 mimetype", func(m map[string]string) { delete(m, "mimetype") }},
		{"wrong mimetype", "invalid EPUB mimetype", "格式标识不正确", func(m map[string]string) { m["mimetype"] = "private-content" }},
		{"large metadata", "oversized EPUB metadata", "大小限制", func(m map[string]string) { m["mimetype"] = strings.Repeat("x", 65) }},
		{"invalid container", "invalid EPUB container", "入口信息无效", func(m map[string]string) { m["META-INF/container.xml"] = "<private-content>" }},
		{"missing package", "missing EPUB package document", "缺少入口指向的 OPF", func(m map[string]string) { delete(m, "EPUB/book.opf") }},
		{"unsafe path", "unsafe EPUB entry", "不安全的文件路径", func(m map[string]string) { m["../private-content"] = "x" }},
	} {
		t.Run(test.name, func(t *testing.T) {
			m := validEPUB()
			test.edit(m)
			assertValidationDiagnostic(t, "private-title.epub", epub(m), test.reason, test.hint)
		})
	}
	t.Run("directory limit", func(t *testing.T) {
		data := epub(validEPUB())
		end := bytes.LastIndex(data, []byte{'P', 'K', 5, 6})
		binary.LittleEndian.PutUint16(data[end+10:], 4097)
		assertValidationDiagnostic(t, "private-title.epub", data, "ZIP directory limit exceeded", "目录大小")
	})
	t.Run("expansion limit", func(t *testing.T) {
		data := epub(validEPUB())
		entry := bytes.Index(data, []byte{'P', 'K', 1, 2})
		binary.LittleEndian.PutUint32(data[entry+24:], 200*1024*1024+1)
		assertValidationDiagnostic(t, "private-title.epub", data, "EPUB expansion limit exceeded", "200 MiB")
	})
	t.Run("PDF end", func(t *testing.T) {
		assertValidationDiagnostic(t, "private-title.pdf", []byte("%PDF-1.4\nprivate-content"), "missing PDF end marker", "结束标记")
	})
}

func assertValidationDiagnostic(t *testing.T, name string, body []byte, reason, hint string) {
	t.Helper()
	s, calls := setup(t)
	var logs bytes.Buffer
	previous := log.Writer()
	log.SetOutput(&logs)
	defer log.SetOutput(previous)
	w := httptest.NewRecorder()
	s.ServeHTTP(w, request(s, name, bytes.NewReader(body)))
	var response map[string]string
	if w.Code != 400 || calls.Load() != 0 || json.Unmarshal(w.Body.Bytes(), &response) != nil {
		t.Fatalf("invalid file was not refused before import: %d", w.Code)
	}
	message := response["message"]
	if !strings.Contains(message, hint) || !strings.Contains(message, "未提交系统导入") || s.transferSnapshot().Message != message {
		t.Fatalf("phone/device diagnostic mismatch: %q", message)
	}
	if !strings.Contains(logs.String(), "reason="+reason) {
		t.Fatal("missing bounded validation reason", logs.String())
	}
	for _, secret := range []string{s.token, "private-title", "private-content", s.spoolDir} {
		if strings.Contains(logs.String(), secret) || strings.Contains(message, secret) {
			t.Fatal("diagnostic leaked request data")
		}
	}
	checkClean(t, s)
	w = httptest.NewRecorder()
	s.ServeHTTP(w, request(s, "next.pdf", strings.NewReader(testPDF)))
	if w.Code != 200 || calls.Load() != 1 {
		t.Fatal("validation failure blocked the next upload")
	}
	checkClean(t, s)
}
