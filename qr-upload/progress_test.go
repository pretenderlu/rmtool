package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestProgressWhileReceivingAndImporting(t *testing.T) {
	s, _ := setup(t)
	entered, release := make(chan struct{}), make(chan struct{})
	original := s.client.Transport
	s.client.Transport = transportFunc(func(r *http.Request) (*http.Response, error) {
		close(entered)
		<-release
		return original.RoundTrip(r)
	})
	reader, writer := io.Pipe()
	defer reader.Close()
	defer writer.Close()
	r := request(s, "progress.pdf", reader)
	r.ContentLength = int64(len(testPDF))
	w := httptest.NewRecorder()
	done := make(chan struct{})
	go func() { s.ServeHTTP(w, r); close(done) }()
	if _, err := writer.Write([]byte(testPDF[:10])); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(time.Second)
	for s.transferSnapshot().Received != 10 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	p := s.transferSnapshot()
	if p.Phase != "receiving" || p.Received != 10 || p.Total != int64(len(testPDF)) || p.Filename != "progress.pdf" {
		t.Errorf("wrong in-flight progress: %+v", p)
	}
	if _, err := writer.Write([]byte(testPDF[10:])); err != nil {
		t.Fatal(err)
	}
	writer.Close()
	select {
	case <-entered:
	case <-time.After(time.Second):
		close(release)
		t.Fatal("import not reached")
	}
	p = s.transferSnapshot()
	if p.Phase != "importing" || p.Received != int64(len(testPDF)) {
		t.Errorf("must not claim completion before native response: %+v", p)
	}
	close(release)
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("upload did not finish")
	}
	if w.Code != 200 || s.transferSnapshot().Phase != "done" {
		t.Fatal("successful native response not reflected", w.Code, s.transferSnapshot())
	}
	checkClean(t, s)
}

func TestProgressErrorsAndUnknownSize(t *testing.T) {
	for _, kind := range []string{"unknown-size", "invalid", "interrupted", "native-error"} {
		t.Run(kind, func(t *testing.T) {
			s, _ := setup(t)
			var body io.Reader = strings.NewReader(testPDF)
			if kind == "invalid" {
				body = strings.NewReader("not a PDF")
			}
			if kind == "interrupted" {
				body = io.MultiReader(strings.NewReader(testPDF[:10]), failingReader{})
			}
			if kind == "native-error" {
				s.client.Transport = transportFunc(func(*http.Request) (*http.Response, error) {
					return nil, errors.New("ambiguous native response")
				})
			}
			r := request(s, "test.pdf", body)
			if kind == "unknown-size" {
				r.ContentLength = -1
			}
			w := httptest.NewRecorder()
			s.ServeHTTP(w, r)
			p := s.transferSnapshot()
			if kind == "unknown-size" {
				if p.Phase != "done" || p.Total != 0 || p.Received != int64(len(testPDF)) {
					t.Fatal(p)
				}
			} else if p.Phase != "error" || !strings.Contains(w.Body.String(), p.Message) {
				t.Fatal("device and phone failure states diverged", p)
			}
			checkClean(t, s)
		})
	}
}

type failingReader struct{}

func (failingReader) Read([]byte) (int, error) { return 0, io.ErrUnexpectedEOF }

func TestBusyOrUnpairedClientCannotChangeOrReadTransfer(t *testing.T) {
	s, _ := setup(t)
	s.progress = transferProgress{Phase: "receiving", Filename: "private.pdf", Received: 10, Total: 100}
	want := s.progress
	s.gate <- struct{}{}
	w := httptest.NewRecorder()
	s.ServeHTTP(w, request(s, "other.pdf", strings.NewReader(testPDF)))
	<-s.gate
	if w.Code != http.StatusTooManyRequests || s.transferSnapshot() != want {
		t.Fatal("rejected upload disturbed active progress")
	}
	for _, auth := range []string{"", "Bearer " + s.token} {
		r := httptest.NewRequest(http.MethodGet, s.origin+"/status", nil)
		r.Header.Set("Authorization", auth)
		w = httptest.NewRecorder()
		s.ServeHTTP(w, r)
		if strings.Contains(w.Body.String(), "private.pdf") || strings.Contains(w.Body.String(), s.token) {
			t.Fatal("private session metadata exposed via status")
		}
	}
}

func TestPrivateSessionProgressAndAtomicReplacement(t *testing.T) {
	s, _ := setup(t)
	// Escaped filenames must fit the native bridge's bounded JSON read.
	s.progress = transferProgress{Phase: "receiving", Filename: strings.Repeat("<", 236) + ".pdf", Received: 10, Total: 50}
	data, err := s.sessionData()
	if err != nil || len(data) > 4096 {
		t.Fatal("session exceeds native read bound", len(data), err)
	}
	var state struct {
		URL      string           `json:"url"`
		Transfer transferProgress `json:"transfer"`
	}
	if err := json.Unmarshal(data, &state); err != nil || state.Transfer != s.progress || !strings.HasSuffix(state.URL, "#"+s.token) {
		t.Fatal("private session did not round trip", err)
	}
	path := filepath.Join(t.TempDir(), "session.json")
	if err := writeSession(path, data); err != nil {
		t.Fatal(err)
	}
	s.transferPhase("done", "finished")
	newData, _ := s.sessionData()
	if err := writeSession(path, newData); err != nil {
		t.Fatal(err)
	}
	got, _ := os.ReadFile(path)
	if !bytes.Equal(got, newData) {
		t.Fatal("session replacement failed")
	}
	if err := os.WriteFile(path+".tmp", []byte("foreign"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := writeSession(path, data); err == nil {
		t.Fatal("overwrote an existing temp file")
	}
	got, _ = os.ReadFile(path)
	foreign, _ := os.ReadFile(path + ".tmp")
	if !bytes.Equal(got, newData) || string(foreign) != "foreign" {
		t.Fatal("failed update changed prior state")
	}
}
