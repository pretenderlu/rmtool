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
	maxDocumentBytes = int64(100_000_000)
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
	if "https://"+r.Host != s.origin || r.URL.RawQuery != "" {
		jsonReply(w, http.StatusBadRequest, "无效的传输地址")
		return
	}
	if origin := r.Header.Get("Origin"); origin != "" && origin != s.origin {
		jsonReply(w, http.StatusForbidden, "请从设备二维码打开上传页面")
		return
	}
	if site := r.Header.Get("Sec-Fetch-Site"); site != "" && site != "same-origin" && site != "none" {
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
	if err != nil || !utf8.ValidString(name) || len(name) == 0 || len(name) > 240 || strings.TrimSpace(name) != name {
		return "", "", errors.New("invalid filename")
	}
	for _, c := range name {
		if c == '/' || c == '\\' || c == ':' || unicode.IsControl(c) || unicode.In(c, unicode.Cf) {
			return "", "", errors.New("unsafe filename")
		}
	}
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
		jsonReply(w, http.StatusBadRequest, "文件名无效，仅支持 PDF 和 EPUB")
		return
	}
	if r.ContentLength == 0 || r.ContentLength > maxDocumentBytes {
		jsonReply(w, http.StatusRequestEntityTooLarge, "文件不能为空或超过 100 MB")
		return
	}
	expected := r.ContentLength
	if expected < 0 {
		expected = maxDocumentBytes
	}
	available, err := s.freeSpace(s.spoolDir)
	if err != nil || available < uint64(expected)*2+storageReserve {
		jsonReply(w, http.StatusInsufficientStorage, "设备空间不足或无法确认剩余空间")
		return
	}
	f, err := os.CreateTemp(s.spoolDir, "upload-*")
	if err != nil {
		jsonReply(w, http.StatusInternalServerError, "设备无法创建接收文件")
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
	size, err := io.Copy(f, r.Body)
	if err != nil {
		var tooLarge *http.MaxBytesError
		if errors.As(err, &tooLarge) {
			jsonReply(w, http.StatusRequestEntityTooLarge, "文件超过 100 MB")
		} else {
			jsonReply(w, http.StatusBadRequest, "文件传输中断，未提交导入")
		}
		return
	}
	if r.ContentLength >= 0 && size != r.ContentLength {
		jsonReply(w, http.StatusBadRequest, "文件传输不完整，未提交导入")
		return
	}
	if err := validateDocument(f, size, kind); err != nil {
		jsonReply(w, http.StatusBadRequest, "文件内容无效或超出当前支持范围，未提交导入")
		return
	}
	available, err = s.freeSpace(s.spoolDir)
	if err != nil || available < uint64(size)+storageReserve {
		jsonReply(w, http.StatusInsufficientStorage, "设备剩余空间不足，未提交导入")
		return
	}
	if !s.active() || ctx.Err() != nil {
		jsonReply(w, http.StatusGone, "接收已结束，未提交导入")
		return
	}
	if err := s.importDocument(ctx, f, size, name); err != nil {
		// A timeout can follow a successful import. Never automatically retry it.
		jsonReply(w, http.StatusBadGateway, "无法确认系统导入结果，请先检查设备资料库，避免重复上传")
		return
	}
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
