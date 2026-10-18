package main

import (
	"context"
	"errors"
	"io"
	"net/http"
	"time"
)

func runManaged(ctx context.Context, address, upstream, spool, state string) (err error) {
	if upstream != "http://10.11.99.1:80" {
		return errors.New("native activation is restricted to the supported device importer")
	}
	cleanup, err := prepareNativeAddress(ctx)
	if err != nil {
		return err
	}
	defer func() { err = errors.Join(err, cleanup()) }()
	return run(ctx, address, upstream, spool, state)
}

func waitNativeImporter(ctx context.Context, client *http.Client, upstream string) error {
	ctx, cancel := context.WithTimeout(ctx, 60*time.Second)
	defer cancel()
	for {
		probe, stop := context.WithTimeout(ctx, 2*time.Second)
		req, err := http.NewRequestWithContext(probe, http.MethodGet, upstream, nil)
		if err != nil {
			stop()
			return err
		}
		response, err := client.Do(req)
		ready := false
		if err == nil {
			_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 1024))
			_ = response.Body.Close()
			ready = response.StatusCode == http.StatusOK
		}
		stop()
		if ready {
			return nil
		}
		select {
		case <-ctx.Done():
			return errors.New("native importer is unavailable; no Wi-Fi receiver was opened")
		case <-time.After(500 * time.Millisecond):
		}
	}
}
