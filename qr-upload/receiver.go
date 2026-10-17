package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/subtle"
	_ "embed"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"mime/multipart"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync"
	"time"
	"unicode"
	"unicode/utf8"
)

const (
	maxDocumentBytes = int64(200_000_000)
	storageReserve   = uint64(64 * 1024 * 1024)
	sessionDuration  = 15 * time.Minute
)

//go:embed web/index.html
var indexHTML []byte

//go:embed web/app.js
var appJS []byte

//go:embed web/style.css
var styleCSS []byte

type receiver struct {
	token      string
	origin     string
	upstream   string
	spoolDir   string
	expires    time.Time
	client     *http.Client
	freeSpace  func(string) (uint64, error)
	gate       chan struct{}
	mu         sync.Mutex
	requests   sync.WaitGroup
	closed     bool
	cancel     context.CancelFunc
	sessionCtx context.Context
	progress   transferProgress
}

type transferProgress struct {
	Phase    string `json:"phase"`
	Filename string `json:"filename"`
	Received int64  `json:"received"`
	Total    int64  `json:"total"`
	Message  string `json:"message"`
}

func (s *receiver) transferSnapshot() transferProgress {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.progress
}

func (s *receiver) transferPhase(phase, message string) {
	s.mu.Lock()
	s.progress.Phase, s.progress.Message = phase, message
	s.mu.Unlock()
}

// Count bytes actually saved on the device, not bytes queued by the phone.
type progressWriter struct {
	file    *os.File
	s       *receiver
	firstAt time.Time
}

func (w *progressWriter) Write(p []byte) (int, error) {
	n, err := w.file.Write(p)
	if n > 0 && w.firstAt.IsZero() {
		w.firstAt = time.Now()
	}
	w.s.mu.Lock()
	w.s.progress.Received += int64(n)
	w.s.mu.Unlock()
	return n, err
}

func newReceiver(origin, upstream, spoolDir string) (*receiver, error) {
	u, err := url.Parse(upstream)
	if err != nil || u.Scheme != "http" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || (u.Path != "" && u.Path != "/") {
		return nil, errors.New("invalid native importer URL")
	}
	if u.Hostname() != "127.0.0.1" && u.Hostname() != "10.11.99.1" {
		return nil, errors.New("native importer must be a fixed device-local address")
	}
	if u.Port() != "" && u.Port() != "80" && u.Port() != "81" {
		return nil, errors.New("unsupported native importer port")
	}
	secret := make([]byte, 32)
	if _, err := rand.Read(secret); err != nil {
		return nil, err
	}
	ctx, cancel := context.WithCancel(context.Background())
	return &receiver{
		token: base64.RawURLEncoding.EncodeToString(secret), origin: origin,
		upstream: strings.TrimRight(upstream, "/") + "/upload", spoolDir: spoolDir,
		expires: time.Now().Add(sessionDuration), gate: make(chan struct{}, 1),
		freeSpace: availableBytes, sessionCtx: ctx, cancel: cancel,
		progress: transferProgress{Phase: "idle", Message: "等待手机传入文档"},
		client: &http.Client{
			Timeout: 2 * time.Minute,
			// Never follow redirects, use environment proxies or forward phone headers.
			Transport:     &http.Transport{Proxy: nil, DisableKeepAlives: true},
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
		},
	}, nil
}

func (s *receiver) stop() {
	s.mu.Lock()
	s.closed = true
	s.cancel()
	s.mu.Unlock()
}

func (s *receiver) active() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return !s.closed && time.Now().Before(s.expires)
}

func jsonReply(w http.ResponseWriter, status int, message string) {
	if status >= 400 {
		log.Printf("Request refused: status=%d", status)
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]string{"message": message})
}

func (s *receiver) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Referrer-Policy", "no-referrer")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("X-Frame-Options", "DENY")
	w.Header().Set("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		jsonReply(w, http.StatusGone, "接收已结束，请在设备上重新开启")
		return
	}
	s.requests.Add(1)
	s.mu.Unlock()
	defer s.requests.Done()
	if "http://"+r.Host != s.origin || r.URL.RawQuery != "" {
		jsonReply(w, http.StatusBadRequest, "无效的传输地址")
		return
	}
	if origin := r.Header.Get("Origin"); origin != "" && origin != s.origin {
		jsonReply(w, http.StatusForbidden, "请从设备二维码打开上传页面")
		return
	}
	// Scanners and external links may navigate cross-site to the public landing page.
	pageNavigation := r.Method == http.MethodGet && r.URL.Path == "/" &&
		r.Header.Get("Sec-Fetch-Mode") == "navigate" && r.Header.Get("Sec-Fetch-Dest") == "document"
	if site := r.Header.Get("Sec-Fetch-Site"); site != "" && site != "same-origin" && site != "none" && !pageNavigation {
		jsonReply(w, http.StatusForbidden, "不允许跨站点请求")
		return
	}
	if !s.active() {
		jsonReply(w, http.StatusGone, "接收已结束，请在设备上重新开启")
		return
	}
	if r.Method == http.MethodGet {
		switch r.URL.Path {
		case "/":
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			_, _ = w.Write(indexHTML)
			return
		case "/app.js":
			w.Header().Set("Content-Type", "text/javascript; charset=utf-8")
			_, _ = w.Write(appJS)
			return
		case "/style.css":
			w.Header().Set("Content-Type", "text/css; charset=utf-8")
			_, _ = w.Write(styleCSS)
			return
		}
	}
	if r.URL.Path != "/upload" && r.URL.Path != "/status" {
		jsonReply(w, http.StatusNotFound, "没有此接口")
		return
	}
	if subtle.ConstantTimeCompare([]byte(r.Header.Get("Authorization")), []byte("Bearer "+s.token)) != 1 {
		jsonReply(w, http.StatusUnauthorized, "链接无效，请重新扫描设备上的二维码")
		return
	}
	if r.Method == http.MethodGet && r.URL.Path == "/status" {
		jsonReply(w, http.StatusOK, "设备已连接")
		return
	}
	if r.Method != http.MethodPost || r.URL.Path != "/upload" {
		jsonReply(w, http.StatusMethodNotAllowed, "不支持此操作")
		return
	}
	s.upload(w, r)
}

func documentName(encoded string) (string, string, error) {
	name, err := url.PathUnescape(encoded)
	if err != nil || !utf8.ValidString(name) || len(name) == 0 || len(name) > 240 {
		return "", "", errors.New("invalid filename")
	}
	for _, c := range name {
		if c == '/' || c == '\\' || c == ':' || unicode.IsControl(c) || unicode.In(c, unicode.Cf) {
			return "", "", errors.New("unsafe filename")
		}
	}
	// Ignore harmless edge spaces, but reject control characters before trimming.
	name = strings.TrimSpace(name)
	ext := ""
	switch {
	case strings.HasSuffix(strings.ToLower(name), ".pdf"):
		ext = "pdf"
	case strings.HasSuffix(strings.ToLower(name), ".epub"):
		ext = "epub"
	}
	if ext == "" || len(name) <= len(ext)+1 || name[0] == '.' {
		return "", "", errors.New("unsupported filename")
	}
	return name, ext, nil
}

func (s *receiver) upload(w http.ResponseWriter, r *http.Request) {
	select {
	case s.gate <- struct{}{}:
		defer func() { <-s.gate }()
	default:
		jsonReply(w, http.StatusTooManyRequests, "正在接收另一个文件，请稍后重试")
		return
	}
	name, kind, err := documentName(r.Header.Get("X-Filename"))
	if err != nil {
		log.Print("Upload admission: invalid filename")
		jsonReply(w, http.StatusBadRequest, "文件名无效，仅支持 PDF 和 EPUB")
		return
	}
	log.Printf("Upload admission: kind=%s content_length=%d", kind, r.ContentLength)
	s.mu.Lock()
	s.progress = transferProgress{Phase: "receiving", Filename: name, Total: max(0, min(r.ContentLength, maxDocumentBytes)), Message: "等待手机发送文件数据"}
	s.mu.Unlock()
	reject := func(code int, message string) {
		s.transferPhase("error", message)
		jsonReply(w, code, message)
	}
	if r.ContentLength == 0 || r.ContentLength > maxDocumentBytes {
		reject(http.StatusRequestEntityTooLarge, "仅支持不超过 200 MB 的非空文件；更大文件请使用 USB 传输")
		return
	}
	expected := r.ContentLength
	if expected < 0 {
		expected = maxDocumentBytes
	}
	available, err := s.freeSpace(s.spoolDir)
	if err != nil || available < uint64(expected)*2+storageReserve {
		reject(http.StatusInsufficientStorage, "设备空间不足或无法确认剩余空间")
		return
	}
	f, err := os.CreateTemp(s.spoolDir, "upload-*")
	if err != nil {
		reject(http.StatusInternalServerError, "设备无法创建接收文件")
		return
	}
	defer func() {
		_ = f.Close()
		if err := os.Remove(f.Name()); err != nil {
			log.Print("Failed to remove an owned temporary upload")
		}
	}()
	ctx, cancel := context.WithCancel(r.Context())
	defer cancel()
	stopCancel := context.AfterFunc(s.sessionCtx, cancel)
	defer stopCancel()
	r.Body = http.MaxBytesReader(w, r.Body, maxDocumentBytes)
	body := r.Body
	stopBody := context.AfterFunc(ctx, func() { _ = body.Close() })
	defer stopBody()
	started := time.Now()
	writer := &progressWriter{file: f, s: s}
	size, err := io.Copy(writer, r.Body)
	firstByteMS := int64(-1)
	if !writer.firstAt.IsZero() {
		firstByteMS = writer.firstAt.Sub(started).Milliseconds()
	}
	log.Printf("Upload receive: bytes=%d first_data_ms=%d receive_ms=%d complete=%t", size, firstByteMS, time.Since(started).Milliseconds(), err == nil)
	if err != nil {
		var tooLarge *http.MaxBytesError
		if errors.As(err, &tooLarge) {
			reject(http.StatusRequestEntityTooLarge, "文件超过 200 MB，请使用 USB 传输")
		} else {
			reject(http.StatusBadRequest, "文件传输中断，未提交导入")
		}
		return
	}
	if r.ContentLength >= 0 && size != r.ContentLength {
		reject(http.StatusBadRequest, "文件传输不完整，未提交导入")
		return
	}
	s.transferPhase("validating", "接收完成，正在校验文件")
	started = time.Now()
	if err := validateDocument(f, size, kind); err != nil {
		message := "文件格式检查未通过"
		var detail documentError
		if errors.As(err, &detail) {
			message = detail.message
			log.Printf("Upload validation rejected: kind=%s bytes=%d reason=%s", kind, size, detail.reason)
		} else {
			log.Printf("Upload validation rejected: kind=%s bytes=%d reason=unknown", kind, size)
		}
		reject(http.StatusBadRequest, message+"，未提交系统导入")
		return
	}
	log.Printf("Upload validation: elapsed_ms=%d", time.Since(started).Milliseconds())
	available, err = s.freeSpace(s.spoolDir)
	if err != nil || available < uint64(size)+storageReserve {
		reject(http.StatusInsufficientStorage, "设备剩余空间不足，未提交导入")
		return
	}
	if !s.active() || ctx.Err() != nil {
		reject(http.StatusGone, "接收已结束，未提交导入")
		return
	}
	s.transferPhase("importing", "正在导入系统资料库")
	started = time.Now()
	err = s.importDocument(ctx, f, size, name)
	log.Printf("Native import: elapsed_ms=%d accepted=%t", time.Since(started).Milliseconds(), err == nil)
	if err != nil {
		// A timeout can follow a successful import. Never automatically retry it.
		reject(http.StatusBadGateway, "无法确认系统导入结果，请先检查设备资料库，避免重复上传")
		return
	}
	s.transferPhase("done", "系统已接受导入，请在资料库查看")
	jsonReply(w, http.StatusOK, "系统已接受导入，请在设备资料库查看")
}

func (s *receiver) importDocument(ctx context.Context, f *os.File, size int64, name string) error {
	var header bytes.Buffer
	writer := multipart.NewWriter(&header)
	if _, err := writer.CreateFormFile("file", name); err != nil {
		return err
	}
	prefix := append([]byte(nil), header.Bytes()...)
	header.Reset()
	if err := writer.Close(); err != nil {
		return err
	}
	suffix := append([]byte(nil), header.Bytes()...)
	body := io.MultiReader(bytes.NewReader(prefix), io.NewSectionReader(f, 0, size), bytes.NewReader(suffix))
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, s.upstream, body)
	if err != nil {
		return err
	}
	req.ContentLength = int64(len(prefix)) + size + int64(len(suffix))
	req.Header.Set("Content-Type", writer.FormDataContentType())
	response, err := s.client.Do(req)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK && response.StatusCode != http.StatusCreated {
		return fmt.Errorf("native importer returned HTTP %d", response.StatusCode)
	}
	_, err = io.Copy(io.Discard, io.LimitReader(response.Body, 64*1024))
	return err
}
