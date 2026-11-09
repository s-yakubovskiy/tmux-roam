package process

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"os/exec"
	"syscall"
	"time"
)

type Result struct {
	Code           int
	Stdout, Stderr []byte
}
type limitedBuffer struct {
	buf bytes.Buffer
	max int
}

func (b *limitedBuffer) Write(p []byte) (int, error) {
	if b.buf.Len()+len(p) > b.max {
		return 0, errors.New("command response exceeded size limit")
	}
	return b.buf.Write(p)
}
func (b *limitedBuffer) Bytes() []byte { return b.buf.Bytes() }

// Capture bounds both output streams and kills the entire child process group on
// cancellation or timeout. Callers pass a deadline; no child survives a failed read.
func Capture(parent context.Context, timeout time.Duration, limit int, argv ...string) (Result, error) {
	return CaptureInput(parent, timeout, limit, nil, argv...)
}

func CaptureInput(parent context.Context, timeout time.Duration, limit int, input io.Reader, argv ...string) (Result, error) {
	var result Result
	if len(argv) == 0 {
		return result, errors.New("empty command")
	}
	ctx, cancel := context.WithTimeout(parent, timeout)
	defer cancel()
	if err := ctx.Err(); err != nil {
		return result, err
	}
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
	// Also bound an internal stdin copier if a multiplex master retains the
	// read end after the SSH client exits. Our captured output pipes are closed
	// explicitly below, before Wait.
	cmd.WaitDelay = 100 * time.Millisecond
	cmd.Stdin = input
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Cancel = func() error { return syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL) }
	out, err := cmd.StdoutPipe()
	if err != nil {
		return result, err
	}
	stderr, err := cmd.StderrPipe()
	if err != nil {
		return result, err
	}
	if err = cmd.Start(); err != nil {
		return result, err
	}
	stdoutBuffer := &limitedBuffer{max: limit}
	stderrBuffer := &limitedBuffer{max: 8192}
	type copyResult struct{ err error }
	done := make(chan copyResult, 2)
	go func() { _, e := io.Copy(stdoutBuffer, out); done <- copyResult{e} }()
	go func() { _, e := io.Copy(stderrBuffer, stderr); done <- copyResult{e} }()
	deadline := ctx.Done()
	for i := 0; i < 2; {
		select {
		case r := <-done:
			i++
			if r.err != nil && err == nil {
				err = r.err
				_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
				_ = out.Close()
				_ = stderr.Close()
			}
		case <-deadline:
			if err == nil {
				err = ctx.Err()
			}
			_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
			// An SSH multiplex master can retain these descriptors outside the
			// child's process group. Cancellation must not wait for its EOF.
			_ = out.Close()
			_ = stderr.Close()
			deadline = nil
		}
	}
	waitErr := cmd.Wait()
	if err == nil && ctx.Err() != nil {
		err = ctx.Err()
	}
	result.Stdout = stdoutBuffer.Bytes()
	result.Stderr = stderrBuffer.Bytes()
	if cmd.ProcessState != nil {
		result.Code = cmd.ProcessState.ExitCode()
	}
	if err != nil {
		return result, err
	}
	if waitErr != nil {
		var exit *exec.ExitError
		if errors.As(waitErr, &exit) {
			return result, nil
		}
		return result, fmt.Errorf("wait: %w", waitErr)
	}
	return result, nil
}
