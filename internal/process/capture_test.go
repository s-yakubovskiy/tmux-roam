package process

import (
	"context"
	"errors"
	"strings"
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
