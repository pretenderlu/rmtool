package main

import (
	"archive/zip"
	"bytes"
	"encoding/binary"
	"encoding/xml"
	"io"
	"os"
	"path"
	"strings"
)

// Keep diagnostics stable and never echo book metadata or parser input.
type documentError struct {
	reason  string
	message string
}

func (e documentError) Error() string { return e.reason }

func validateDocument(f *os.File, size int64, kind string) error {
	if size <= 0 || size > maxDocumentBytes {
		return documentError{"invalid document size", "仅支持不超过 200 MB 的非空文件；更大文件请使用 USB 传输"}
	}
	if kind == "pdf" {
		prefix := make([]byte, 8)
		if _, err := f.ReadAt(prefix, 0); err != nil || !bytes.HasPrefix(prefix, []byte("%PDF-")) {
			return documentError{"missing PDF signature", "未找到 PDF 文件头，请确认文件格式"}
		}
		start := size - 1024
		if start < 0 {
			start = 0
		}
		tail := make([]byte, size-start)
		if _, err := f.ReadAt(tail, start); err != nil || !bytes.Contains(tail, []byte("%%EOF")) {
			return documentError{"missing PDF end marker", "未找到 PDF 结束标记，文件可能不完整"}
		}
		return nil // Native import, not a second PDF parser, owns document semantics.
	}
	if kind != "epub" {
		return documentError{"unsupported document type", "仅支持 PDF 和 EPUB 文件"}
	}
	// Bound archive/zip's central-directory allocation before it parses entries.
	start := size - (65535 + 22)
	if start < 0 {
		start = 0
	}
	tail := make([]byte, size-start)
	if _, err := f.ReadAt(tail, start); err != nil {
		return documentError{"ZIP directory read failed", "设备无法读取已接收文件"}
	}
	end := -1
	for i := len(tail) - 22; i >= 0; i-- {
		if bytes.Equal(tail[i:i+4], []byte{'P', 'K', 5, 6}) && i+22+int(binary.LittleEndian.Uint16(tail[i+20:])) == len(tail) {
			end = i
			break
		}
	}
	if end < 0 {
		return documentError{"missing ZIP directory", "EPUB 压缩包目录缺失或格式不受支持"}
	}
	eocd := tail[end:]
	entries := binary.LittleEndian.Uint16(eocd[10:])
	directorySize := binary.LittleEndian.Uint32(eocd[12:])
	directoryOffset := binary.LittleEndian.Uint32(eocd[16:])
	if entries > 4096 || directorySize > 2*1024*1024 {
		return documentError{"ZIP directory limit exceeded", "EPUB 内部文件数量或目录大小超出当前接收限制"}
	}
	if binary.LittleEndian.Uint32(eocd[4:]) != 0 || entries != binary.LittleEndian.Uint16(eocd[8:]) || entries == 0 || directoryOffset == 0xffffffff || uint64(directoryOffset)+uint64(directorySize) != uint64(start+int64(end)) {
		return documentError{"unsupported ZIP directory layout", "EPUB 压缩包目录结构异常或不受支持"}
	}
	z, err := zip.NewReader(f, size)
	if err != nil {
		return documentError{"invalid ZIP archive", "无法解析 EPUB 压缩包，请检查文件是否完整"}
	}
	if len(z.File) != int(entries) {
		return documentError{"inconsistent ZIP entry count", "EPUB 压缩包目录与实际文件数量不一致"}
	}
	files := make(map[string]*zip.File, len(z.File))
	var expanded uint64
	for _, entry := range z.File {
		name := strings.TrimSuffix(entry.Name, "/")
		// ZIP members stay on Linux; reject drive/URI prefixes, not nested punctuation.
		first, _, _ := strings.Cut(name, "/")
		if name == "" || path.Clean(name) != name || strings.HasPrefix(name, "/") || strings.HasPrefix(name, "../") || name == ".." || strings.ContainsAny(name, "\\\x00") || strings.Contains(first, ":") || entry.Mode()&os.ModeSymlink != 0 {
			return documentError{"unsafe EPUB entry", "EPUB 内部包含不安全的文件路径或符号链接"}
		}
		if entry.Flags&1 != 0 {
			return documentError{"encrypted EPUB entry", "暂不支持带 ZIP 加密的 EPUB"}
		}
		if _, exists := files[entry.Name]; exists {
			return documentError{"duplicate EPUB entry", "EPUB 压缩包存在同名文件，无法安全导入"}
		}
		files[entry.Name] = entry
		if entry.UncompressedSize64 > 200*1024*1024-expanded {
			return documentError{"EPUB expansion limit exceeded", "EPUB 解压后超过当前 200 MiB 接收限制"}
		}
		expanded += entry.UncompressedSize64
	}
	mimetype, err := readEntry(files["mimetype"], 64)
	if err != nil {
		return err
	}
	if string(mimetype) != "application/epub+zip" {
		return documentError{"invalid EPUB mimetype", "EPUB 的 mimetype 格式标识不正确"}
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
		return documentError{"invalid EPUB container", "EPUB 的 container.xml 入口信息无效"}
	}
	for _, file := range root.Files {
		entry := files[file.FullPath]
		if entry == nil || entry.FileInfo().IsDir() {
			return documentError{"missing EPUB package document", "EPUB 缺少入口指向的 OPF 文件"}
		}
	}
	return nil
}

func readEntry(f *zip.File, limit int64) ([]byte, error) {
	if f == nil {
		return nil, documentError{"missing EPUB metadata", "EPUB 缺少 mimetype 或 META-INF/container.xml"}
	}
	if f.UncompressedSize64 > uint64(limit) {
		return nil, documentError{"oversized EPUB metadata", "EPUB 的格式标识或入口信息超出当前大小限制"}
	}
	r, err := f.Open()
	if err != nil {
		return nil, documentError{"unsupported EPUB metadata compression", "EPUB 元数据压缩格式不受支持或已损坏"}
	}
	defer r.Close()
	data, err := io.ReadAll(io.LimitReader(r, limit+1))
	if err != nil || int64(len(data)) > limit {
		return nil, documentError{"invalid EPUB metadata", "EPUB 元数据无法完整解压或校验失败"}
	}
	return data, nil
}
