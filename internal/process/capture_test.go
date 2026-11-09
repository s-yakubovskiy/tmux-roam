package process

import (
	"bytes"
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

func TestCaptureOutputAndLimit(t *testing.T) {
	r, err := Capture(context.Background(), time.Second, 100, "sh", "-c", "printf hello; printf warning >&2")
	if err != nil || r.Code != 0 || string(r.Stdout) != "hello" || string(r.Stderr) != "warning" {
		t.Fatalf("%+v %v", r, err)
	}
	_, err = Capture(context.Background(), time.Second, 10, "sh", "-c", "printf 12345678901234567890")
	if err == nil || !strings.Contains(err.Error(), "size limit") {
		t.Fatalf("expected output limit, got %v", err)
	}
}
func TestCaptureCancelsDescendant(t *testing.T) {
	start := time.Now()
	_, err := Capture(context.Background(), 100*time.Millisecond, 100, "sh", "-c", "sleep 10 & wait")
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("expected deadline, got %v", err)
	}
	if time.Since(start) > time.Second {
		t.Fatal("descendant kept pipes open after timeout")
	}
}

func TestCaptureCancelsAfterOutputClosed(t *testing.T) {
	start := time.Now()
	_, err := Capture(context.Background(), 100*time.Millisecond, 100, "sh", "-c", "exec 1>&- 2>&-; sleep 10 & wait")
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("expected deadline, got %v", err)
	}
	if time.Since(start) > time.Second {
		t.Fatal("closed output bypassed timeout")
	}
}

func TestCaptureCancellationDoesNotWaitForForeignPipeWriter(t *testing.T) {
	if _, err := exec.LookPath("setsid"); err != nil {
		t.Skip("setsid unavailable")
	}
	pidfile := filepath.Join(t.TempDir(), "writer.pid")
	stopWriter := func() {
		data, _ := os.ReadFile(pidfile)
		pid, _ := strconv.Atoi(strings.TrimSpace(string(data)))
		if pid > 0 {
			_ = syscall.Kill(pid, syscall.SIGKILL)
		}
	}
	t.Cleanup(stopWriter)
	// A broken cancellation path still ends promptly enough to report a
	// useful failure, rather than waiting for the foreign process's sleep.
	timer := time.AfterFunc(time.Second, stopWriter)
	defer timer.Stop()
	start := time.Now()
	_, err := CaptureInput(context.Background(), 100*time.Millisecond, 100, bytes.NewReader(make([]byte, 1<<20)), "sh", "-c",
		`exec 3<&0; setsid sh -c 'echo $$ > "$1"; exec sleep 30' sh "$1" <&3 & wait`, "sh", pidfile)
	if !errors.Is(err, context.DeadlineExceeded) || time.Since(start) > 500*time.Millisecond {
		t.Fatalf("foreign writer delayed cancellation: %v (%s)", err, time.Since(start))
	}
}
