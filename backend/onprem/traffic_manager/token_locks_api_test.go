package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func lockAPIManager(t *testing.T, script string) *manager {
	t.Helper()
	m := testManager(t)
	m.scripts = map[string]string{digest([]byte(script)): script}
	m.batchScripts = map[string]bool{}
	previous := log.Writer()
	log.SetOutput(io.Discard)
	t.Cleanup(func() { log.SetOutput(previous) })
	return m
}

func lockAPIRequest(m *manager, token string) request {
	r := reservation{Token: token, Schema: "cityverse.detached-shard/v1", SelectedRole: "server",
		SelectedUID: m.uid, SelectedBoot: m.boot, SelectedNetns: m.netns,
		Roles: []role{{Role: "server", UID: m.uid, Pod: m.pod, Namespace: m.namespace, Boot: m.boot, Netns: m.netns}}}
	raw, _ := json.Marshal(r)
	q := testRequest(m, "capability")
	q.Encoded = base64.StdEncoding.EncodeToString(raw)
	return q
}

func lockAPIPost(m *manager, q request) int {
	raw, _ := json.Marshal(q)
	req := httptest.NewRequest("POST", "/v1/control", strings.NewReader(string(raw)))
	req.Header.Set("Authorization", "Bearer "+m.token)
	response := httptest.NewRecorder()
	m.ServeHTTP(response, req)
	return response.Code
}

func lockCount(m *manager) int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return len(m.locks)
}

func TestHTTPUniqueCapabilityLocksReleaseOnSuccessAndError(t *testing.T) {
	for _, item := range []struct {
		name, script string
		status       int
	}{{"success", "exit 0", 200}, {"worker-error", "exit 5", 409}} {
		t.Run(item.name, func(t *testing.T) {
			m := lockAPIManager(t, item.script)
			for i := 0; i < 270; i++ {
				q := lockAPIRequest(m, fmt.Sprintf("%032x", i+1))
				if got := lockAPIPost(m, q); got != item.status {
					t.Fatalf("request %d status %d, expected %d", i, got, item.status)
				}
				if count := lockCount(m); count != 0 {
					t.Fatalf("request %d retained %d completed token locks", i, count)
				}
			}
		})
	}
}

func TestHTTPQueuedSameTokenKeepsOneMutexAndSerializes(t *testing.T) {
	dir := t.TempDir()
	busy := filepath.Join(dir, "busy")
	overlap := filepath.Join(dir, "overlap")
	script := fmt.Sprintf("mkdir '%s' || { touch '%s'; exit 5; }; sleep .03; rmdir '%s'", busy, overlap, busy)
	m := lockAPIManager(t, script)
	token := strings.Repeat("d", 32)
	release, err := m.acquireToken(context.Background(), token)
	if err != nil {
		t.Fatal(err)
	}
	original := m.locks[token]
	var workers sync.WaitGroup
	statuses := make(chan int, 4)
	for i := 0; i < 4; i++ {
		workers.Add(1)
		go func() {
			defer workers.Done()
			statuses <- lockAPIPost(m, lockAPIRequest(m, token))
		}()
	}
	deadline := time.Now().Add(2 * time.Second)
	registered := false
	for time.Now().Before(deadline) {
		m.mu.Lock()
		registered = m.locks[token] == original && original.refs == 5
		m.mu.Unlock()
		if registered {
			break
		}
		time.Sleep(time.Millisecond)
	}
	release()
	workers.Wait()
	close(statuses)
	if !registered {
		t.Fatal("queued HTTP waiters were not registered on the same token entry")
	}
	for status := range statuses {
		if status != 200 {
			t.Fatalf("serialized capability returned %d", status)
		}
	}
	if _, err := os.Stat(overlap); !os.IsNotExist(err) {
		t.Fatal("same-token workers overlapped")
	}
	if lockCount(m) != 0 {
		t.Fatal("drained waiters retained token entry")
	}
}

func TestHTTPTokenCapacityStaysBoundedWithOutstandingHolders(t *testing.T) {
	m := lockAPIManager(t, "exit 0")
	releases := make([]func(), 0, 256)
	defer func() {
		for _, release := range releases {
			if release != nil {
				release()
			}
		}
	}()
	for i := 0; i < 256; i++ {
		release, err := m.acquireToken(context.Background(), fmt.Sprintf("%032x", i+1))
		if err != nil {
			t.Fatal(err)
		}
		releases = append(releases, release)
	}
	q := lockAPIRequest(m, strings.Repeat("e", 32))
	if got := lockAPIPost(m, q); got != http.StatusConflict {
		t.Fatalf("overflow returned %d", got)
	}
	if lockCount(m) != 256 {
		t.Fatal("capacity changed while holders were outstanding")
	}
	releases[0]()
	releases[0] = nil
	if got := lockAPIPost(m, q); got != http.StatusOK {
		t.Fatalf("released slot did not recover: %d", got)
	}
	for i, release := range releases {
		if release != nil {
			release()
			releases[i] = nil
		}
	}
	if lockCount(m) != 0 {
		t.Fatal("completed holder capacity was not reclaimed")
	}
}

func TestHTTPCanceledTokenWaiterReleasesItsReference(t *testing.T) {
	m := lockAPIManager(t, "exit 0")
	token := strings.Repeat("f", 32)
	release, err := m.acquireToken(context.Background(), token)
	if err != nil {
		t.Fatal(err)
	}
	q := lockAPIRequest(m, token)
	q.Timeout = .02
	if got := lockAPIPost(m, q); got != http.StatusConflict {
		release()
		t.Fatalf("canceled waiter returned %d", got)
	}
	m.mu.Lock()
	refs := m.locks[token].refs
	m.mu.Unlock()
	release()
	if refs != 1 || lockCount(m) != 0 {
		t.Fatal("canceled waiter leaked or evicted the holder reference")
	}
}
