package main

import (
	"archive/zip"
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"io"
	"mime"
	"mime/multipart"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

const testPDF = "%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n"

func TestParentExitCancelsSession(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var parent atomic.Int32
	parent.Store(123)
	tick := make(chan time.Time)
	checked := make(chan struct{}, 2)
	done := make(chan struct{})
	go func() {
		watchParent(ctx, cancel, 123, func() int {
			value := parent.Load()
			checked <- struct{}{}
			return int(value)
		}, tick)
		close(done)
	}()
	tick <- time.Now()
	<-checked
	if ctx.Err() != nil {
		t.Fatal("live owner canceled the session")
	}
	parent.Store(1)
	tick <- time.Now()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("orphaned receiver did not stop")
	}
	if ctx.Err() == nil {
		t.Fatal("owner exit left uploads authorized")
	}
}

type transportFunc func(*http.Request) (*http.Response, error)

func (f transportFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func setup(t *testing.T) (*receiver, *atomic.Int32) {
	t.Helper()
	s, err := newReceiver("http://192.168.1.2:8765", "http://127.0.0.1:80", t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(s.stop)
	s.freeSpace = func(string) (uint64, error) { return 1024 * 1024 * 1024, nil }
	calls := new(atomic.Int32)
	s.client.Transport = transportFunc(func(r *http.Request) (*http.Response, error) {
		calls.Add(1)
		if r.URL.String() != "http://127.0.0.1:80/upload" || r.Method != "POST" {
			t.Error("wrong native import endpoint")
		}
		if r.Header.Get("Authorization") != "" || r.Header.Get("Cookie") != "" || r.Header.Get("X-Filename") != "" {
			t.Error("phone headers leaked to importer")
		}
		data, err := io.ReadAll(r.Body)
		if err != nil || int64(len(data)) != r.ContentLength {
			t.Error("multipart length mismatch", err)
		}
		r.Body = io.NopCloser(bytes.NewReader(data))
		_, params, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
		if err != nil {
			t.Fatal(err)
		}
		reader := multipart.NewReader(bytes.NewReader(data), params["boundary"])
		part, err := reader.NextPart()
		if err != nil || part.FormName() != "file" {
			t.Fatal("native file field missing", err)
		}
		payload, err := io.ReadAll(part)
		if err != nil || len(payload) == 0 {
			t.Fatal("missing document payload", err)
		}
		if _, err := reader.NextPart(); err != io.EOF {
			t.Error("unexpected second multipart field")
		}
		return &http.Response{StatusCode: http.StatusCreated, Body: io.NopCloser(strings.NewReader("{}")), Header: make(http.Header)}, nil
	})
	return s, calls
}

func request(s *receiver, name string, body io.Reader) *http.Request {
	r := httptest.NewRequest(http.MethodPost, s.origin+"/upload", body)
	r.Header.Set("Authorization", "Bearer "+s.token)
	r.Header.Set("X-Filename", url.PathEscape(name))
	r.Header.Set("Origin", s.origin)
	return r
}

func checkClean(t *testing.T, s *receiver) {
	t.Helper()
	files, err := os.ReadDir(s.spoolDir)
	if err != nil || len(files) != 0 {
		t.Errorf("temporary uploads leaked: %v, %v", files, err)
	}
}

func epub(entries map[string]string) []byte {
	var data bytes.Buffer
	z := zip.NewWriter(&data)
	for name, body := range entries {
		f, err := z.Create(name)
		if err != nil {
			panic(err)
		}
		if _, err := io.WriteString(f, body); err != nil {
			panic(err)
		}
	}
	if err := z.Close(); err != nil {
		panic(err)
	}
	return data.Bytes()
}

func validEPUB() map[string]string {
	return map[string]string{
		"mimetype":               "application/epub+zip",
		"META-INF/container.xml": `<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="EPUB/book.opf"/></rootfiles></container>`,
		"EPUB/book.opf":          `<package xmlns="http://www.idpf.org/2007/opf" version="3.0"/>`,
	}
}

func TestUpload(t *testing.T) {
	for _, test := range []struct {
		name string
		data []byte
	}{
		{"中文 & \"摘录\".PDF", []byte(testPDF)}, {"书籍.epub", epub(validEPUB())},
		{" 书籍.epub ", epub(validEPUB())},
	} {
		t.Run(test.name, func(t *testing.T) {
			s, calls := setup(t)
			original := s.client.Transport
			s.client.Transport = transportFunc(func(r *http.Request) (*http.Response, error) {
				data, err := io.ReadAll(r.Body)
				if err != nil {
					t.Fatal(err)
				}
				r.Body = io.NopCloser(bytes.NewReader(data))
				mr, err := r.MultipartReader()
				if err != nil {
					t.Fatal(err)
				}
				part, err := mr.NextPart()
				if err != nil || part.FileName() != strings.TrimSpace(test.name) {
					t.Fatalf("filename changed: %v", err)
				}
				got, _ := io.ReadAll(part)
				if !bytes.Equal(got, test.data) {
					t.Error("payload changed")
				}
				r.Body = io.NopCloser(bytes.NewReader(data))
				return original.RoundTrip(r)
			})
			w := httptest.NewRecorder()
			s.ServeHTTP(w, request(s, test.name, bytes.NewReader(test.data)))
			if w.Code != http.StatusOK || calls.Load() != 1 {
				t.Fatalf("HTTP %d: %s", w.Code, w.Body.String())
			}
			checkClean(t, s)
		})
	}
}

func TestRefusalsNeverImport(t *testing.T) {
	tests := []struct {
		name   string
		mutate func(*receiver, *http.Request)
		code   int
	}{
		{"missing token", func(_ *receiver, r *http.Request) { r.Header.Del("Authorization") }, 401},
		{"wrong token", func(_ *receiver, r *http.Request) { r.Header.Set("Authorization", "Bearer wrong") }, 401},
		{"expired", func(s *receiver, _ *http.Request) { s.expires = time.Now().Add(-time.Second) }, 410},
		{"stopped", func(s *receiver, _ *http.Request) { s.stop() }, 410},
		{"other origin", func(_ *receiver, r *http.Request) { r.Header.Set("Origin", "https://evil.example") }, 403},
		{"null origin", func(_ *receiver, r *http.Request) { r.Header.Set("Origin", "null") }, 403},
		{"wrong scheme", func(_ *receiver, r *http.Request) { r.Header.Set("Origin", "https://"+r.Host) }, 403},
		{"cross site", func(_ *receiver, r *http.Request) { r.Header.Set("Sec-Fetch-Site", "cross-site") }, 403},
		{"wrong host", func(_ *receiver, r *http.Request) { r.Host = "evil.example" }, 400},
		{"token in query", func(_ *receiver, r *http.Request) { r.URL.RawQuery = "token=secret" }, 400},
		{"too large", func(_ *receiver, r *http.Request) { r.ContentLength = maxDocumentBytes + 1 }, 413},
		{"empty", func(_ *receiver, r *http.Request) { r.ContentLength = 0 }, 413},
		{"truncated", func(_ *receiver, r *http.Request) { r.ContentLength += 1 }, 400},
		{"bad signature", func(_ *receiver, r *http.Request) {
			r.Body = io.NopCloser(strings.NewReader(strings.Repeat("x", len(testPDF))))
		}, 400},
		{"unknown capacity", func(s *receiver, _ *http.Request) {
			s.freeSpace = func(string) (uint64, error) { return 0, errors.New("probe failed") }
		}, 507},
		{"low capacity", func(s *receiver, _ *http.Request) {
			s.freeSpace = func(string) (uint64, error) { return storageReserve, nil }
		}, 507},
		{"missing spool", func(s *receiver, _ *http.Request) { s.spoolDir = filepath.Join(s.spoolDir, "missing") }, 500},
		{"downloads blocked", func(_ *receiver, r *http.Request) { r.Method = "GET"; r.URL.Path = "/documents/" }, 404},
		{"deletion blocked", func(_ *receiver, r *http.Request) { r.Method = "DELETE" }, 405},
		{"preflight blocked", func(_ *receiver, r *http.Request) { r.Method = "OPTIONS" }, 405},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			s, calls := setup(t)
			r := request(s, "book.pdf", strings.NewReader(testPDF))
			test.mutate(s, r)
			w := httptest.NewRecorder()
			s.ServeHTTP(w, r)
			if w.Code != test.code || calls.Load() != 0 {
				t.Fatalf("HTTP %d, calls %d, want %d", w.Code, calls.Load(), test.code)
			}
			if test.name != "missing spool" {
				checkClean(t, s)
			}
		})
	}
}

func TestCrossSitePageNavigation(t *testing.T) {
	tests := []struct {
		name, method, path, mode, destination, site string
		mutate                                      func(*receiver, *http.Request)
		code                                        int
	}{
		{"scan landing", "GET", "/", "navigate", "document", "cross-site", nil, 200},
		{"same-site link", "GET", "/", "navigate", "document", "same-site", nil, 200},
		{"iframe", "GET", "/", "navigate", "iframe", "cross-site", nil, 403},
		{"fetch landing", "GET", "/", "cors", "empty", "cross-site", nil, 403},
		{"missing navigation metadata", "GET", "/", "", "", "cross-site", nil, 403},
		{"status navigation", "GET", "/status", "navigate", "document", "cross-site", nil, 403},
		{"upload navigation", "POST", "/upload", "navigate", "document", "cross-site", nil, 403},
		{"form landing", "POST", "/", "navigate", "document", "cross-site", nil, 403},
		{"script fetch", "GET", "/app.js", "no-cors", "script", "cross-site", nil, 403},
		{"foreign origin", "GET", "/", "navigate", "document", "cross-site", func(_ *receiver, r *http.Request) { r.Header.Set("Origin", "https://other.example") }, 403},
		{"wrong host", "GET", "/", "navigate", "document", "cross-site", func(_ *receiver, r *http.Request) { r.Host = "other.example" }, 400},
		{"query token", "GET", "/?token=secret", "navigate", "document", "cross-site", nil, 400},
		{"expired", "GET", "/", "navigate", "document", "cross-site", func(s *receiver, _ *http.Request) { s.expires = time.Now().Add(-time.Second) }, 410},
		{"stopped", "GET", "/", "navigate", "document", "cross-site", func(s *receiver, _ *http.Request) { s.stop() }, 410},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			s, calls := setup(t)
			r := httptest.NewRequest(test.method, s.origin+test.path, nil)
			r.Header.Set("Sec-Fetch-Mode", test.mode)
			r.Header.Set("Sec-Fetch-Dest", test.destination)
			r.Header.Set("Sec-Fetch-Site", test.site)
			if test.mutate != nil {
				test.mutate(s, r)
			}
			w := httptest.NewRecorder()
			s.ServeHTTP(w, r)
			if w.Code != test.code || calls.Load() != 0 {
				t.Fatalf("HTTP %d, imports %d, want %d", w.Code, calls.Load(), test.code)
			}
			if test.code == 200 && (!bytes.Equal(w.Body.Bytes(), indexHTML) || strings.Contains(w.Body.String(), s.token)) {
				t.Fatal("landing must serve only the public page, without credentials")
			}
			if w.Header().Get("X-Frame-Options") != "DENY" || w.Header().Get("Access-Control-Allow-Origin") != "" {
				t.Fatal("navigation exception must not allow framing or CORS")
			}
		})
	}
}

func TestFilename(t *testing.T) {
	for _, name := range []string{"../book.pdf", "a/b.pdf", `a\b.pdf`, "a\x00.pdf", "a\r\n.pdf", "a\u202e.pdf", "book.txt", ".pdf", "x:book.pdf", " .pdf ", "\nbook.pdf", "book.pdf\t", " \u202e书籍.pdf", strings.Repeat("中", 90) + ".pdf"} {
		if _, _, err := documentName(url.PathEscape(name)); err == nil {
			t.Errorf("accepted unsafe filename %q", name)
		}
	}
	for _, encoded := range []string{"%GG.pdf", "%FF.pdf"} {
		if _, _, err := documentName(encoded); err == nil {
			t.Error("accepted malformed encoding")
		}
	}
	for _, name := range []string{" book.pdf", "book.pdf ", "　中文.epub　"} {
		got, _, err := documentName(url.PathEscape(name))
		if err != nil || got != strings.TrimSpace(name) {
			t.Errorf("filename edge spaces were not normalized: %q, %v", got, err)
		}
	}
}

func TestCapacityRechecked(t *testing.T) {
	s, calls := setup(t)
	n := 0
	s.freeSpace = func(string) (uint64, error) {
		n++
		if n == 1 {
			return 1 << 30, nil
		}
		return 0, nil
	}
	w := httptest.NewRecorder()
	s.ServeHTTP(w, request(s, "book.pdf", strings.NewReader(testPDF)))
	if w.Code != 507 || calls.Load() != 0 || n != 2 {
		t.Fatal("capacity was not checked before import")
	}
	checkClean(t, s)
}

type zeroReader struct{}

func (zeroReader) Read(p []byte) (int, error) { clear(p); return len(p), nil }

func TestUploadSizeBoundary(t *testing.T) {
	for _, test := range []struct {
		name    string
		size    int64
		chunked bool
		code    int
	}{
		{"200 MB", 200_000_000, false, 200},
		{"200 MB chunked", 200_000_000, true, 200},
		{"one byte over", 200_000_001, false, 413},
	} {
		t.Run(test.name, func(t *testing.T) {
			s, calls := setup(t)
			s.client.Transport = transportFunc(func(r *http.Request) (*http.Response, error) {
				calls.Add(1)
				parts, err := r.MultipartReader()
				if err != nil {
					t.Fatal(err)
				}
				part, err := parts.NextPart()
				if err != nil || part.FormName() != "file" {
					t.Fatal("missing native document", err)
				}
				if n, err := io.Copy(io.Discard, part); err != nil || n != test.size {
					t.Fatalf("native payload size %d, want %d: %v", n, test.size, err)
				}
				return &http.Response{StatusCode: 201, Body: io.NopCloser(strings.NewReader("{}"))}, nil
			})
			prefix, suffix := "%PDF-1.4\n", "\n%%EOF\n"
			body := io.MultiReader(strings.NewReader(prefix),
				io.LimitReader(zeroReader{}, test.size-int64(len(prefix)+len(suffix))), strings.NewReader(suffix))
			r := request(s, "boundary.pdf", body)
			r.ContentLength = test.size
			if test.chunked {
				r.ContentLength = -1
			}
			w := httptest.NewRecorder()
			s.ServeHTTP(w, r)
			if w.Code != test.code {
				t.Fatalf("HTTP %d, want %d: %s", w.Code, test.code, w.Body.String())
			}
			if test.code == 200 {
				if calls.Load() != 1 || s.progress.Received != test.size {
					t.Fatal("accepted upload must retain full progress and import exactly once")
				}
			} else if calls.Load() != 0 || !strings.Contains(w.Body.String(), "200 MB") || !strings.Contains(w.Body.String(), "USB") {
				t.Fatal("oversized upload must refuse import and recommend USB")
			}
			checkClean(t, s)
		})
	}
}

func TestChunkedUploadCap(t *testing.T) {
	s, calls := setup(t)
	w := httptest.NewRecorder()
	r := request(s, "book.pdf", io.LimitReader(zeroReader{}, maxDocumentBytes+1))
	r.ContentLength = -1
	s.ServeHTTP(w, r)
	if w.Code != 413 || calls.Load() != 0 {
		t.Fatal("unknown-length upload escaped size cap")
	}
	if !strings.Contains(w.Body.String(), "200 MB") || !strings.Contains(w.Body.String(), "USB") {
		t.Fatal("oversized unknown-length upload must recommend USB")
	}
	checkClean(t, s)
}

type brokenReader struct{}

func (brokenReader) Read([]byte) (int, error) { return 0, io.ErrUnexpectedEOF }

func TestInterruptedAndValidChunkedUploads(t *testing.T) {
	for _, broken := range []bool{false, true} {
		s, calls := setup(t)
		var data io.Reader = strings.NewReader(testPDF)
		if broken {
			data = io.MultiReader(strings.NewReader(testPDF), brokenReader{})
		}
		r := request(s, "book.pdf", data)
		r.ContentLength = -1
		w := httptest.NewRecorder()
		s.ServeHTTP(w, r)
		if broken && (w.Code != 400 || calls.Load() != 0) {
			t.Fatal("interrupted input was imported")
		}
		if !broken && (w.Code != 200 || calls.Load() != 1) {
			t.Fatal("valid unknown-length upload failed")
		}
		checkClean(t, s)
	}
}

func TestConnectionLimitAndClose(t *testing.T) {
	listener, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	l := &limitedListener{Listener: listener, slots: make(chan struct{}, 1), done: make(chan struct{})}
	defer l.Close()
	client, err := net.Dial("tcp4", listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	c, err := l.Accept()
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	if len(l.slots) != 1 {
		t.Fatal("connection not accounted for")
	}
	_ = c.Close()
	_ = c.Close()
	if len(l.slots) != 0 {
		t.Fatal("connection slot not released exactly once")
	}
	l.slots <- struct{}{}
	result := make(chan error, 1)
	go func() { _, err := l.Accept(); result <- err }()
	_ = l.Close()
	select {
	case err := <-result:
		if !errors.Is(err, net.ErrClosed) {
			t.Fatal("blocked accept was not canceled", err)
		}
	case <-time.After(time.Second):
		t.Fatal("listener close did not unblock full connection limit")
	}
}

func TestNativeFailureNoRetry(t *testing.T) {
	for _, status := range []int{0, 302, 413, 500} {
		t.Run(http.StatusText(status), func(t *testing.T) {
			s, _ := setup(t)
			calls := 0
			s.client.Transport = transportFunc(func(r *http.Request) (*http.Response, error) {
				calls++
				if status == 0 {
					return nil, errors.New("lost response")
				}
				return &http.Response{StatusCode: status, Body: io.NopCloser(strings.NewReader("untrusted upstream error")), Header: http.Header{"Location": []string{"http://evil.example"}}}, nil
			})
			w := httptest.NewRecorder()
			s.ServeHTTP(w, request(s, "book.pdf", strings.NewReader(testPDF)))
			if w.Code != 502 || calls != 1 || strings.Contains(w.Body.String(), "untrusted") {
				t.Fatal("failure retry or error disclosure")
			}
			checkClean(t, s)
		})
	}
}

func TestConcurrentUploadAndStop(t *testing.T) {
	s, _ := setup(t)
	started := make(chan struct{})
	s.client.Transport = transportFunc(func(r *http.Request) (*http.Response, error) {
		close(started)
		<-r.Context().Done()
		return nil, r.Context().Err()
	})
	done := make(chan int, 1)
	go func() {
		w := httptest.NewRecorder()
		s.ServeHTTP(w, request(s, "first.pdf", strings.NewReader(testPDF)))
		done <- w.Code
	}()
	select {
	case <-started:
	case <-time.After(time.Second):
		t.Fatal("first import did not start")
	}
	w := httptest.NewRecorder()
	s.ServeHTTP(w, request(s, "second.pdf", strings.NewReader(testPDF)))
	if w.Code != 429 {
		t.Fatal("concurrent upload accepted")
	}
	s.stop()
	select {
	case code := <-done:
		if code != 502 {
			t.Fatalf("wrong canceled import result: %d", code)
		}
	case <-time.After(time.Second):
		t.Fatal("stop did not cancel native import")
	}
	checkClean(t, s)
}

func TestRequestCanceledBeforeImport(t *testing.T) {
	s, calls := setup(t)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	w := httptest.NewRecorder()
	s.ServeHTTP(w, request(s, "book.pdf", strings.NewReader(testPDF)).WithContext(ctx))
	if w.Code != 410 || calls.Load() != 0 {
		t.Fatal("canceled request imported")
	}
	checkClean(t, s)
}

func TestEPUBValidation(t *testing.T) {
	tests := []struct {
		name string
		edit func(map[string]string)
	}{
		{"missing mimetype", func(m map[string]string) { delete(m, "mimetype") }},
		{"wrong mimetype", func(m map[string]string) { m["mimetype"] = "application/zip" }},
		{"missing container", func(m map[string]string) { delete(m, "META-INF/container.xml") }},
		{"bad container", func(m map[string]string) { m["META-INF/container.xml"] = "<container>" }},
		{"missing package", func(m map[string]string) { delete(m, "EPUB/book.opf") }},
		{"traversal", func(m map[string]string) { m["../escaped"] = "bad" }},
		{"absolute path", func(m map[string]string) { m["/etc/passwd"] = "bad" }},
		{"backslash", func(m map[string]string) { m[`EPUB\escape`] = "bad" }},
		{"drive path", func(m map[string]string) { m["C:/escaped"] = "bad" }},
		{"drive relative path", func(m map[string]string) { m["C:escaped"] = "bad" }},
		{"URL path", func(m map[string]string) { m["https:/example.invalid/book"] = "bad" }},
		{"nested traversal", func(m map[string]string) { m["EPUB/../../escaped"] = "bad" }},
		{"oversized metadata", func(m map[string]string) { m["mimetype"] = strings.Repeat("x", 65) }},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			s, calls := setup(t)
			m := validEPUB()
			test.edit(m)
			w := httptest.NewRecorder()
			s.ServeHTTP(w, request(s, "book.epub", bytes.NewReader(epub(m))))
			if w.Code != 400 || calls.Load() != 0 {
				t.Fatal("unsafe EPUB accepted")
			}
			checkClean(t, s)
		})
	}
	for _, offset := range []int{10, 12, 16} {
		s, calls := setup(t)
		data := epub(validEPUB())
		end := bytes.LastIndex(data, []byte{'P', 'K', 5, 6})
		if offset == 10 {
			binary.LittleEndian.PutUint16(data[end+offset:], 65535)
		} else if offset == 12 {
			binary.LittleEndian.PutUint32(data[end+offset:], 4*1024*1024)
		} else {
			binary.LittleEndian.PutUint32(data[end+offset:], 0xffffffff)
		}
		w := httptest.NewRecorder()
		s.ServeHTTP(w, request(s, "book.epub", bytes.NewReader(data)))
		if w.Code != 400 || calls.Load() != 0 {
			t.Fatal("unbounded ZIP directory accepted")
		}
	}
}

func TestLocalOnlyUpstreamAndListener(t *testing.T) {
	for _, u := range []string{"https://127.0.0.1", "http://evil.example", "http://127.0.0.1:9999", "http://user:password@127.0.0.1", "http://127.0.0.1/admin", "http://127.0.0.1/?x=1"} {
		if s, err := newReceiver("http://192.168.1.2:8765", u, t.TempDir()); err == nil {
			s.stop()
			t.Fatalf("accepted upstream %s", u)
		}
	}
	for _, address := range []string{"0.0.0.0:8765", ":8765", "8.8.8.8:8765", "127.0.0.1:8765", "host.local:8765", "192.168.1.2:80"} {
		if _, err := listenIP(address); err == nil {
			t.Errorf("accepted listener %s", address)
		}
	}
	if _, err := listenIP("192.168.1.2:8765"); err != nil {
		t.Fatal(err)
	}
}

func TestTokenAndHTTP(t *testing.T) {
	s, calls := setup(t)
	other, _ := setup(t)
	if s.token == other.token || len(s.token) != 43 {
		t.Fatal("session tokens are not independent 256-bit values")
	}
	server := httptest.NewServer(s)
	defer server.Close()
	s.origin = server.URL
	for _, test := range []struct {
		token string
		code  int
	}{{"", 401}, {other.token, 401}, {s.token, 200}} {
		req, _ := http.NewRequest("GET", server.URL+"/status", nil)
		// Plain HTTP browsers may omit Fetch Metadata; the token must still be required.
		if test.token != "" {
			req.Header.Set("Authorization", "Bearer "+test.token)
		}
		res, err := server.Client().Do(req)
		if err != nil {
			t.Fatal(err)
		}
		res.Body.Close()
		if res.StatusCode != test.code || res.TLS != nil || res.Header.Get("Access-Control-Allow-Origin") != "" {
			t.Fatalf("HTTP/auth boundary failed: %d, want %d", res.StatusCode, test.code)
		}
	}
	req := request(s, "http.pdf", strings.NewReader(testPDF))
	req.RequestURI = ""
	res, err := server.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	res.Body.Close()
	if res.StatusCode != 200 || calls.Load() != 1 {
		t.Fatal("plain HTTP upload failed")
	}
	checkClean(t, s)
}

func TestStaticPageHasNoCredentialsOrCDN(t *testing.T) {
	s, calls := setup(t)
	for _, route := range []string{"/", "/app.js", "/style.css"} {
		w := httptest.NewRecorder()
		s.ServeHTTP(w, httptest.NewRequest("GET", s.origin+route, nil))
		if w.Code != 200 || strings.Contains(w.Body.String(), s.token) || w.Header().Get("Referrer-Policy") != "no-referrer" {
			t.Fatal("static page exposes secret or misses security headers")
		}
	}
	if calls.Load() != 0 {
		t.Fatal("static pages contacted importer")
	}
	if !bytes.Contains(indexHTML, []byte("传输未加密，仅限可信 Wi-Fi")) {
		t.Fatal("missing trusted-network warning")
	}
	if bytes.Contains(indexHTML, []byte("https://")) || bytes.Contains(appJS, []byte("innerHTML")) || bytes.Contains(appJS, []byte("localStorage")) {
		t.Fatal("remote asset, unsafe filename rendering or token persistence")
	}
	var body map[string]string
	w := httptest.NewRecorder()
	s.stop()
	s.ServeHTTP(w, httptest.NewRequest("GET", s.origin+"/", nil))
	if w.Code != 410 || json.Unmarshal(w.Body.Bytes(), &body) != nil {
		t.Fatal("stopped session still serves page")
	}
}
