// A persistent, fixed-command control plane for existing detached iperf workers.
package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"embed"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

// These files are exported from the immutable Python command builder at build.
//
//go:embed scripts/*.sh
var scripts embed.FS

const maxBody = 100 * 1024

var tokenPattern = regexp.MustCompile(`^[a-f0-9]{32}$`)
var actions = map[string]bool{"identity": true, "absence": true, "capability": true, "start": true,
	"status": true, "release": true, "collect": true, "cancel": true}

type request struct {
	Mode      string  `json:"mode,omitempty"`
	Token     string  `json:"token,omitempty"`
	Action    string  `json:"action"`
	Encoded   string  `json:"encoded"`
	Epoch     int64   `json:"epoch"`
	ScriptSHA string  `json:"scriptSha256"`
	Timeout   float64 `json:"timeout"`
	PodUID    string  `json:"podUid"`
}
type role struct {
	Role      string `json:"role"`
	Pod       string `json:"pod"`
	Namespace string `json:"namespace"`
	UID       string `json:"podUid"`
	Boot      string `json:"bootId"`
	Netns     string `json:"netns"`
}
type reservation struct {
	Token         string `json:"token"`
	Schema        string `json:"schemaVersion"`
	SelectedRole  string `json:"selectedRole"`
	SelectedUID   string `json:"selectedPodUid"`
	SelectedBoot  string `json:"selectedBootId"`
	SelectedNetns string `json:"selectedNetns"`
	Roles         []role `json:"roles"`
}
type tokenLock struct {
	mutex sync.Mutex
	refs  int // Active holder plus all registered waiters, protected by manager.mu.
}
type manager struct {
	token, uid, pod, namespace, boot, netns string
	scripts                                 map[string]string
	batchScripts                            map[string]bool
	tunedSHA, capabilityScript              string
	mu                                      sync.Mutex
	locks                                   map[string]*tokenLock
	children                                map[int]bool
	slots                                   chan struct{}
	longSlots                               chan struct{}
}

func read(path string, maximum int) ([]byte, error) {
	info, err := os.Lstat(path)
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() || info.Size() > int64(maximum) {
		return nil, errors.New("state unavailable")
	}
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	b, err := io.ReadAll(io.LimitReader(f, int64(maximum)+1))
	if len(b) > maximum {
		return nil, errors.New("state oversized")
	}
	return b, err
}
func digest(b []byte) string { v := sha256.Sum256(b); return hex.EncodeToString(v[:]) }

func newManager(token, uid, pod, namespace string) (*manager, error) {
	if len(token) != 64 || uid == "" || pod == "" || namespace == "" {
		return nil, errors.New("configuration missing")
	}
	if _, err := hex.DecodeString(token); err != nil {
		return nil, errors.New("configuration invalid")
	}
	b, err := read("/proc/sys/kernel/random/boot_id", 64)
	if err != nil {
		return nil, err
	}
	netns, err := os.Readlink("/proc/self/ns/net")
	if err != nil {
		return nil, err
	}
	m := &manager{token: token, uid: uid, pod: pod, namespace: namespace,
		boot: strings.TrimSpace(string(b)), netns: netns, scripts: map[string]string{},
		locks: map[string]*tokenLock{}, children: map[int]bool{}, slots: make(chan struct{}, 8),
		batchScripts: map[string]bool{}, longSlots: make(chan struct{}, 16)}
	for _, name := range []string{"legacy", "tuned", "batch", "batch-free-ran", "batch-shard", "batch-shard-free-ran"} {
		b, err := scripts.ReadFile("scripts/" + name + ".sh")
		if err != nil {
			return nil, err
		}
		m.scripts[digest(b)] = string(b)
		if name == "tuned" {
			m.tunedSHA = digest(b)
		}
		if strings.HasPrefix(name, "batch") {
			m.batchScripts[digest(b)] = true
		}
	}
	probe, err := scripts.ReadFile("scripts/tuned-capability.sh")
	if err != nil {
		return nil, err
	}
	m.capabilityScript = string(probe)
	return m, nil
}

func (m *manager) validate(q request) (reservation, []byte, error) {
	var r reservation
	if q.Mode == "batch" {
		valid := q.Action == "server" || q.Action == "client" || q.Action == "ready" || q.Action == "client_ready" || q.Action == "release" || q.Action == "cancel" || q.Action == "status"
		_, err := base64.StdEncoding.Strict().DecodeString(q.Encoded)
		if !valid || !m.batchScripts[q.ScriptSHA] || q.PodUID != m.uid || !tokenPattern.MatchString(q.Token) || q.Epoch < 0 || q.Timeout <= 0 || q.Timeout > 52 || err != nil {
			return r, nil, errors.New("invalid batch request")
		}
		return r, nil, nil
	}
	if q.Mode != "" || q.Token != "" || m.batchScripts[q.ScriptSHA] {
		return r, nil, errors.New("invalid mode")
	}
	if !actions[q.Action] || q.PodUID != m.uid || q.Timeout <= 0 || q.Timeout > 10 ||
		m.scripts[q.ScriptSHA] == "" || q.Epoch < 0 || (q.Action == "release") != (q.Epoch > 0) {
		return r, nil, errors.New("invalid request")
	}
	if q.Action == "identity" || q.Action == "absence" {
		if q.Encoded != "-" || q.Epoch != 0 {
			return r, nil, errors.New("invalid identity")
		}
		return r, nil, nil
	}
	b, err := base64.StdEncoding.Strict().DecodeString(q.Encoded)
	if err != nil || len(b) > 65536 || json.Unmarshal(b, &r) != nil || !tokenPattern.MatchString(r.Token) ||
		r.SelectedUID != m.uid || r.SelectedBoot != m.boot || r.SelectedNetns != m.netns ||
		(r.SelectedRole != "server" && r.SelectedRole != "client") {
		return r, nil, errors.New("invalid reservation")
	}
	n := 0
	for _, v := range r.Roles {
		if v.UID == m.uid && v.Role == r.SelectedRole {
			if v.Pod != m.pod || v.Namespace != m.namespace || v.Boot != m.boot || v.Netns != m.netns {
				return r, nil, errors.New("foreign reservation")
			}
			n++
		}
	}
	if n != 1 {
		return r, nil, errors.New("ambiguous reservation")
	}
	if r.Schema != "cityverse.detached-shard/v1" && r.Schema != "cityverse.detached-shard/v2" {
		return r, nil, errors.New("unknown schema")
	}
	if (r.Schema == "cityverse.detached-shard/v2") !=
		strings.Contains(m.scripts[q.ScriptSHA], "deadline_clock_before()") {
		return r, nil, errors.New("script version mismatch")
	}
	return r, b, nil
}

func (m *manager) status(r reservation, raw []byte) (string, error) {
	dir := "/tmp/cityverse-detached-" + r.Token
	if info, err := os.Lstat(dir); err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return "", errors.New("state unavailable")
	}
	b, err := read(dir+"/reservation.json", 65536)
	if err != nil || !bytes.Equal(b, raw) {
		return "", errors.New("reservation changed")
	}
	sha := digest(raw)
	claim, err := read(dir+"/claim", 256)
	if err != nil || string(claim) != fmt.Sprintf("%s %s %s\n", sha, m.boot, m.netns) {
		return "", errors.New("claim changed")
	}
	p, err := read(dir+"/process", 128)
	if err != nil {
		return "", err
	}
	fields := strings.Fields(string(p))
	if len(fields) != 3 {
		return "", errors.New("invalid process")
	}
	for _, f := range fields {
		v, err := strconv.ParseUint(f, 10, 63)
		if err != nil || v == 0 {
			return "", errors.New("invalid process")
		}
	}
	state := "ARMED"
	for _, v := range []struct{ file, state string }{{"seal", "SEALED"}, {"cancelled", "CANCELLED"}, {"failed", "FAILED"}, {"listening", "LISTENING"}, {"release", "RUNNING"}} {
		if info, err := os.Lstat(dir + "/" + v.file); err == nil {
			if !info.Mode().IsRegular() {
				return "", errors.New("invalid marker")
			}
			state = v.state
			break
		} else if !os.IsNotExist(err) {
			return "", err
		}
	}
	release := "-"
	if b, err := read(dir+"/release", 32); err == nil {
		release = strings.TrimSpace(string(b))
		if v, err := strconv.ParseUint(release, 10, 63); err != nil || v == 0 {
			return "", errors.New("invalid release")
		}
	} else if !os.IsNotExist(err) {
		return "", err
	}
	if state == "ARMED" || state == "LISTENING" || state == "RUNNING" {
		b, err := read("/proc/"+fields[0]+"/stat", 8192)
		if err != nil {
			return "", err
		}
		i := strings.LastIndexByte(string(b), ')')
		if i < 0 {
			return "", errors.New("invalid proc")
		}
		v := strings.Fields(string(b[i+1:]))
		if len(v) < 20 || v[0] == "Z" || v[2] != fields[2] || v[19] != fields[1] || fields[0] != fields[2] {
			return "", errors.New("process identity changed")
		}
	}
	return fmt.Sprintf("STATUSv1 %s %s %s %s %s %s %s %s %s\n", r.Token, state, sha,
		fields[0], fields[1], fields[2], m.boot, m.netns, release), nil
}

// boundedBuffer never accumulates an unbounded worker stdout/stderr stream.
type boundedBuffer struct {
	bytes.Buffer
	maximum int
}

func (b *boundedBuffer) Write(p []byte) (int, error) {
	if b.Len()+len(p) > b.maximum {
		return 0, errors.New("worker output exceeded bound")
	}
	return b.Buffer.Write(p)
}
func (m *manager) acquireToken(ctx context.Context, token string) (func(), error) {
	m.mu.Lock()
	entry := m.locks[token]
	if entry == nil {
		if len(m.locks) >= 256 {
			m.mu.Unlock()
			return nil, errors.New("manager reservation capacity exhausted")
		}
		entry = &tokenLock{}
		m.locks[token] = entry
	}
	entry.refs++ // Register before waiting: holders must not evict a waiter's lock.
	m.mu.Unlock()
	unref := func() {
		m.mu.Lock()
		entry.refs--
		if entry.refs == 0 && m.locks[token] == entry {
			delete(m.locks, token)
		}
		m.mu.Unlock()
	}
	for !entry.mutex.TryLock() {
		select {
		case <-ctx.Done():
			unref()
			return nil, ctx.Err()
		case <-time.After(time.Millisecond):
		}
	}
	return func() {
		entry.mutex.Unlock()
		unref()
	}, nil
}

func (m *manager) execute(ctx context.Context, q request) (string, error) {
	r, raw, err := m.validate(q)
	if err != nil {
		return "", err
	}
	if q.Mode == "batch" {
		return m.run(ctx, q, []string{"-c", m.scripts[q.ScriptSHA], "cityverse-batch", q.Action, q.Token, strconv.FormatInt(q.Epoch, 10), q.Encoded})
	}
	if q.Action == "identity" {
		return fmt.Sprintf("IDENTITYv1 %s %s\n", m.boot, m.netns), nil
	}
	if q.Action == "absence" {
		paths, err := filepath.Glob("/proc/[0-9]*/comm")
		if err != nil {
			return "", err
		}
		for _, path := range paths {
			b, err := read(path, 256)
			if os.IsNotExist(err) {
				continue
			}
			if err != nil {
				return "", err
			}
			if strings.TrimSpace(string(b)) == "iperf3" {
				return "busy", nil
			}
		}
		return "absent", nil
	}
	if q.Action == "status" {
		return m.status(r, raw)
	}
	release, err := m.acquireToken(ctx, r.Token)
	if err != nil {
		return "", err
	}
	defer release()
	if err := ctx.Err(); err != nil {
		return "", err
	}
	if q.Action == "capability" && r.Schema == "cityverse.detached-shard/v2" &&
		q.ScriptSHA == m.tunedSHA && m.capabilityScript != "" {
		// Bash command substitution in the full worker removes all trailing LF.
		// Preserve those exact bytes and the original child sed/identity checks;
		// do not re-encode JSON or format the receipt from parsed fields.
		raw = bytes.TrimRight(raw, "\n")
		return m.run(ctx, q, []string{"-c", m.capabilityScript, "cityverse-capability",
			q.Action, string(raw), strconv.FormatInt(q.Epoch, 10), digest(raw)})
	}
	return m.run(ctx, q, []string{"-c", m.scripts[q.ScriptSHA], "cityverse-detached", q.Action, q.Encoded, strconv.FormatInt(q.Epoch, 10)})
}

func (m *manager) run(ctx context.Context, q request, args []string) (string, error) {
	cmd := exec.CommandContext(ctx, "bash", args...)
	out, stderr := &boundedBuffer{maximum: 7*1024*1024 + 512}, &boundedBuffer{maximum: 1024}
	cmd.Stdout, cmd.Stderr = out, stderr
	cmd.WaitDelay = 100 * time.Millisecond
	dispatched := time.Now()
	m.mu.Lock()
	err := cmd.Start()
	if err == nil {
		m.children[cmd.Process.Pid] = true
	}
	m.mu.Unlock()
	if err != nil {
		return "", errors.New("worker dispatch failed")
	}
	log.Printf("traffic control action=%s phase=spawn elapsedSeconds=%.6f", q.Action, time.Since(dispatched).Seconds())
	err = cmd.Wait()
	m.mu.Lock()
	delete(m.children, cmd.Process.Pid)
	m.mu.Unlock()
	if err != nil {
		code := -1
		var exited *exec.ExitError
		if errors.As(err, &exited) {
			code = exited.ExitCode()
		}
		log.Printf("traffic control action=%s phase=wait elapsedSeconds=%.6f exitCode=%d deadline=%t", q.Action, time.Since(dispatched).Seconds(), code, ctx.Err() != nil)
		return "", errors.New("worker command failed")
	}
	log.Printf("traffic control action=%s phase=complete elapsedSeconds=%.6f", q.Action, time.Since(dispatched).Seconds())
	return out.String(), nil
}

func (m *manager) ServeHTTP(w http.ResponseWriter, req *http.Request) {
	if req.Method == "GET" && req.URL.RequestURI() == "/healthz" {
		w.Write([]byte("ok\n"))
		return
	}
	if req.Method != "POST" || req.URL.RequestURI() != "/v1/control" {
		http.Error(w, "not found", 404)
		return
	}
	if subtle.ConstantTimeCompare([]byte(req.Header.Get("Authorization")), []byte("Bearer "+m.token)) != 1 {
		http.Error(w, "unauthorized", 401)
		return
	}
	var q request
	decoder := json.NewDecoder(http.MaxBytesReader(w, req.Body, maxBody))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&q) != nil || decoder.Decode(new(any)) != io.EOF {
		http.Error(w, "invalid request", 400)
		return
	}
	if _, _, err := m.validate(q); err != nil {
		http.Error(w, "invalid control", 400)
		return
	}
	ctx, cancel := context.WithTimeout(req.Context(), time.Duration(q.Timeout*float64(time.Second)))
	defer cancel()
	slots := m.slots
	if q.Mode == "batch" && (q.Action == "server" || q.Action == "client") {
		slots = m.longSlots
	}
	select {
	case slots <- struct{}{}:
		defer func() { <-slots }()
	case <-ctx.Done():
		http.Error(w, "deadline", 504)
		return
	}
	result, err := m.execute(ctx, q)
	if err != nil || ctx.Err() != nil {
		// Log only fixed failure reasons / filesystem errors; never request bytes,
		// authorization headers, worker output, or the management credential.
		log.Printf("traffic control action=%s phase=reject reason=%q deadline=%t", q.Action, err, ctx.Err() != nil)
		http.Error(w, "control unavailable", 409)
		return
	}
	w.Header().Set("Content-Type", "text/plain; charset=us-ascii")
	w.Header().Set("Content-Length", strconv.Itoa(len(result)))
	io.WriteString(w, result)
}

func (m *manager) reap() {
	entries, _ := os.ReadDir("/proc")
	for _, entry := range entries {
		pid, err := strconv.Atoi(entry.Name())
		if err != nil {
			continue
		}
		b, err := read(filepath.Join("/proc", entry.Name(), "stat"), 8192)
		if err != nil {
			continue
		}
		i := strings.LastIndexByte(string(b), ')')
		if i < 0 {
			continue
		}
		v := strings.Fields(string(b[i+1:]))
		if len(v) < 2 || v[0] != "Z" || v[1] != strconv.Itoa(os.Getpid()) {
			continue
		}
		m.mu.Lock()
		if !m.children[pid] {
			var status syscall.WaitStatus
			syscall.Wait4(pid, &status, syscall.WNOHANG, nil)
		}
		m.mu.Unlock()
	}
}

func (m *manager) shutdown() {
	// Only exact reservations owned by this Pod/boot/netns can reach cancel.
	paths, _ := filepath.Glob("/tmp/cityverse-detached-*/reservation.json")
	var wg sync.WaitGroup
	for _, path := range paths {
		raw, err := read(path, 65536)
		if err != nil {
			continue
		}
		var r reservation
		if json.Unmarshal(raw, &r) != nil {
			continue
		}
		for sha, script := range m.scripts {
			if m.batchScripts[sha] {
				continue
			}
			if (r.Schema == "cityverse.detached-shard/v2") != strings.Contains(script, "deadline_clock_before()") {
				continue
			}
			q := request{Action: "cancel", Encoded: base64.StdEncoding.EncodeToString(raw), ScriptSHA: sha, Timeout: 5, PodUID: m.uid}
			if _, _, err := m.validate(q); err != nil {
				continue
			}
			wg.Add(1)
			go func(q request) {
				defer wg.Done()
				ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
				defer cancel()
				m.execute(ctx, q)
			}(q)
		}
	}
	wg.Wait()
	m.reap()
}

func main() {
	m, err := newManager(os.Getenv("CITYVERSE_TRAFFIC_MANAGER_TOKEN"), os.Getenv("POD_UID"), os.Getenv("POD_NAME"), os.Getenv("POD_NAMESPACE"))
	if err != nil {
		fmt.Fprintln(os.Stderr, "traffic manager configuration unavailable")
		os.Exit(1)
	}
	server := &http.Server{Addr: ":9084", Handler: m, ReadHeaderTimeout: 2 * time.Second,
		ReadTimeout: 3 * time.Second, WriteTimeout: 53 * time.Second, IdleTimeout: 120 * time.Second, MaxHeaderBytes: 4096}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	go func() {
		gcTicker := time.NewTicker(30 * time.Second)
		defer gcTicker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-gcTicker.C:
				if removed := m.gcClosed("/tmp"); removed > 0 {
					log.Printf("traffic closed-reservation gc removed=%d", removed)
				}
			case <-time.After(time.Second):
				m.reap()
			}
		}
	}()
	finished := make(chan struct{})
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 12*time.Second)
		defer cancel()
		server.Shutdown(shutdown)
		m.shutdown()
		close(finished)
	}()
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		fmt.Fprintln(os.Stderr, "traffic manager listener unavailable")
		os.Exit(1)
	}
	<-finished
}
