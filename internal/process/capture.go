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
	var result Result
	if len(argv) == 0 {
		return result, errors.New("empty command")
	}
	ctx, cancel := context.WithTimeout(parent, timeout)
	defer cancel()
	if err := ctx.Err(); err != nil {
		return result, err
	}
	cmd := exec.Command(argv[0], argv[1:]...)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
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
			}
		case <-deadline:
			if err == nil {
				err = ctx.Err()
			}
			_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
			deadline = nil
		}
	}
	waitErr := cmd.Wait()
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
