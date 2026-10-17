package main

import (
	"archive/zip"
	"bytes"
	"encoding/binary"
	"encoding/xml"
	"errors"
	"io"
	"os"
	"path"
	"strings"
)

func validateDocument(f *os.File, size int64, kind string) error {
	if size <= 0 || size > maxDocumentBytes {
		return errors.New("invalid document size")
	}
	if kind == "pdf" {
		prefix := make([]byte, 8)
		if _, err := f.ReadAt(prefix, 0); err != nil || !bytes.HasPrefix(prefix, []byte("%PDF-")) {
			return errors.New("missing PDF signature")
		}
		start := size - 1024
		if start < 0 {
			start = 0
		}
		tail := make([]byte, size-start)
		if _, err := f.ReadAt(tail, start); err != nil || !bytes.Contains(tail, []byte("%%EOF")) {
			return errors.New("missing PDF end marker")
		}
		return nil // Native import, not a second PDF parser, owns document semantics.
	}
	if kind != "epub" {
		return errors.New("unsupported document type")
	}
	// Bound archive/zip's central-directory allocation before it parses entries.
	start := size - (65535 + 22)
	if start < 0 {
		start = 0
	}
	tail := make([]byte, size-start)
	if _, err := f.ReadAt(tail, start); err != nil {
		return err
	}
	end := -1
	for i := len(tail) - 22; i >= 0; i-- {
		if bytes.Equal(tail[i:i+4], []byte{'P', 'K', 5, 6}) && i+22+int(binary.LittleEndian.Uint16(tail[i+20:])) == len(tail) {
			end = i
			break
		}
	}
	if end < 0 {
		return errors.New("missing ZIP directory")
	}
	eocd := tail[end:]
	entries := binary.LittleEndian.Uint16(eocd[10:])
	directorySize := binary.LittleEndian.Uint32(eocd[12:])
	directoryOffset := binary.LittleEndian.Uint32(eocd[16:])
	if binary.LittleEndian.Uint32(eocd[4:]) != 0 || entries != binary.LittleEndian.Uint16(eocd[8:]) || entries == 0 || entries > 4096 || directorySize > 2*1024*1024 || directoryOffset == 0xffffffff || uint64(directoryOffset)+uint64(directorySize) != uint64(start+int64(end)) {
		return errors.New("unsupported ZIP directory size or layout")
	}
	z, err := zip.NewReader(f, size)
	if err != nil {
		return err
	}
	if len(z.File) != int(entries) {
		return errors.New("inconsistent ZIP entry count")
	}
	files := make(map[string]*zip.File, len(z.File))
	var expanded uint64
	for _, entry := range z.File {
		name := strings.TrimSuffix(entry.Name, "/")
		if name == "" || path.Clean(name) != name || strings.HasPrefix(name, "/") || strings.HasPrefix(name, "../") || name == ".." || strings.ContainsAny(name, "\\\x00:") || entry.Mode()&os.ModeSymlink != 0 || entry.Flags&1 != 0 {
			return errors.New("unsafe or encrypted EPUB entry")
		}
		if _, exists := files[entry.Name]; exists {
			return errors.New("duplicate EPUB entry")
		}
		files[entry.Name] = entry
		if entry.UncompressedSize64 > 200*1024*1024-expanded {
			return errors.New("EPUB expansion limit exceeded")
		}
		expanded += entry.UncompressedSize64
	}
	mimetype, err := readEntry(files["mimetype"], 64)
	if err != nil || string(mimetype) != "application/epub+zip" {
		return errors.New("missing EPUB mimetype")
	}
	container, err := readEntry(files["META-INF/container.xml"], 64*1024)
	if err != nil {
		return err
	}
	var root struct {
		XMLName xml.Name `xml:"container"`
		Files   []struct {
			FullPath string `xml:"full-path,attr"`
		} `xml:"rootfiles>rootfile"`
	}
	if err := xml.Unmarshal(container, &root); err != nil || len(root.Files) == 0 {
		return errors.New("invalid EPUB container")
	}
	for _, file := range root.Files {
		entry := files[file.FullPath]
		if entry == nil || entry.FileInfo().IsDir() {
			return errors.New("missing EPUB package document")
		}
	}
	return nil
}

func readEntry(f *zip.File, limit int64) ([]byte, error) {
	if f == nil || f.UncompressedSize64 > uint64(limit) {
		return nil, errors.New("missing or oversized EPUB metadata")
	}
	r, err := f.Open()
	if err != nil {
		return nil, err
	}
	defer r.Close()
	data, err := io.ReadAll(io.LimitReader(r, limit+1))
	if err != nil || int64(len(data)) > limit {
		return nil, errors.New("invalid EPUB metadata")
	}
	return data, nil
}
