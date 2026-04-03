package delivery

import (
	"io"
	"sync"
)

// copyBufPool reuses 256KB buffers across io.Copy calls to reduce GC pressure
// and syscall count (256KB vs default 32KB = 8x fewer syscalls per chunk).
var copyBufPool = sync.Pool{
	New: func() any {
		b := make([]byte, 256*1024)
		return &b
	},
}

// copyBuffer copies from src to dst using a pooled 256KB buffer.
func copyBuffer(dst io.Writer, src io.Reader) (int64, error) {
	bufp := copyBufPool.Get().(*[]byte)
	n, err := io.CopyBuffer(dst, src, *bufp)
	copyBufPool.Put(bufp)
	return n, err
}
