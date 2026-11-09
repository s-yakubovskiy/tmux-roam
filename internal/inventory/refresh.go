package inventory

import (
	"context"
	"sync"

	"code.talos.lowlife.us/baza/tmux-roam/internal/config"
	"code.talos.lowlife.us/baza/tmux-roam/internal/model"
)

type HostResult struct {
	Host    config.Host
	Records []model.Record
	Err     error
}

type refreshCall struct {
	done   chan struct{}
	result HostResult
}

// Refresher belongs to one CLI/picker invocation. Overlapping requests for the
// same host share one observation. A cancelled waiter never cancels another
// waiter's observation; Close cancels and joins all owned workers and children.
// Separate processes coordinate only their short cache publications.
type Refresher struct {
	ctx     context.Context
	cancel  context.CancelFunc
	config  config.Config
	update  bool
	sem     chan struct{}
	mu      sync.Mutex
	pending map[config.Host]*refreshCall
	closed  bool
	wg      sync.WaitGroup
}

func NewRefresher(ctx context.Context, c config.Config, update bool) *Refresher {
	ctx, cancel := context.WithCancel(ctx)
	return &Refresher{ctx: ctx, cancel: cancel, config: c, update: update,
		sem: make(chan struct{}, max(1, c.Concurrency)), pending: map[config.Host]*refreshCall{}}
}

func (r *Refresher) Close() {
	r.mu.Lock()
	r.closed = true
	r.cancel()
	r.mu.Unlock()
	r.wg.Wait()
}

func (r *Refresher) Refresh(ctx context.Context, host config.Host) HostResult {
	r.mu.Lock()
	if r.closed || r.ctx.Err() != nil || ctx.Err() != nil {
		r.mu.Unlock()
		err := ctx.Err()
		if err == nil {
			err = r.ctx.Err()
		}
		if err == nil {
			err = context.Canceled
		}
		return HostResult{Host: host, Err: err}
	}
	call := r.pending[host]
	if call == nil {
		call = &refreshCall{done: make(chan struct{})}
		r.pending[host] = call
		r.wg.Add(1)
		go r.observe(host, call)
	}
	r.mu.Unlock()
	select {
	case <-ctx.Done():
		return HostResult{Host: host, Err: ctx.Err()}
	case <-call.done:
		if err := ctx.Err(); err != nil {
			return HostResult{Host: host, Err: err}
		}
		return call.result
	}
}

func (r *Refresher) observe(host config.Host, call *refreshCall) {
	defer r.wg.Done()
	result := HostResult{Host: host}
	select {
	case r.sem <- struct{}{}:
		result.Records, result.Err = RemoteWithUpdate(r.ctx, host, r.config, r.update)
		if result.Err == nil {
			result.Err = SaveContext(r.ctx, host, result.Records)
		} else if r.ctx.Err() == nil {
			result.Records = Cached(host)
		}
		<-r.sem
	case <-r.ctx.Done():
		result.Err = r.ctx.Err()
	}
	r.mu.Lock()
	call.result = result
	delete(r.pending, host)
	close(call.done)
	r.mu.Unlock()
}

// RefreshHosts delivers hosts in completion order on the caller's goroutine.
// A slow host occupies only its own concurrency slot; cancellation suppresses
// subsequent callbacks and all workers are joined before returning.
func RefreshHosts(ctx context.Context, c config.Config, update bool, publish func(HostResult)) {
	r := NewRefresher(ctx, c, update)
	defer r.Close()
	results := make(chan HostResult, len(c.Hosts))
	var wg sync.WaitGroup
	for _, host := range c.Hosts {
		if !host.Enabled {
			continue
		}
		wg.Add(1)
		go func(host config.Host) {
			defer wg.Done()
			results <- r.Refresh(ctx, host)
		}(host)
	}
	go func() { wg.Wait(); close(results) }()
	for result := range results {
		if ctx.Err() == nil {
			publish(result)
		}
	}
}
