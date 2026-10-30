package main

import (
	"encoding/base64"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func testManager(t *testing.T) *manager {
	t.Helper()
	m, err := newManager(strings.Repeat("a", 64), "test-pod-uid", "test-pod", "test-namespace")
	if err != nil {
		t.Fatal(err)
	}
	return m
}
func testRequest(m *manager, action string) request {
	for sha, s := range m.scripts {
		if !m.batchScripts[sha] && !strings.Contains(s, "deadline_clock_before()") {
			return request{Action: action, Encoded: "-", ScriptSHA: sha, Timeout: 2, PodUID: m.uid}
		}
	}
	panic("legacy script missing")
}

func TestHTTPKeepAliveAndRejection(t *testing.T) {
	m := testManager(t)
	server := httptest.NewUnstartedServer(m)
	var opened atomic.Int32
	server.Config.ConnState = func(_ net.Conn, state http.ConnState) {
		if state == http.StateNew {
			opened.Add(1)
		}
	}
	server.Start()
	defer server.Close()
	client := server.Client()
	defer client.CloseIdleConnections()
	q := testRequest(m, "identity")
	for i := 0; i < 4; i++ {
		v := q
		auth := "Bearer " + m.token
		if i == 2 {
			auth = "wrong"
		}
		if i == 3 {
			v.PodUID = "foreign"
		}
		raw, _ := json.Marshal(v)
		r, _ := http.NewRequest("POST", server.URL+"/v1/control", strings.NewReader(string(raw)))
		r.Header.Set("Authorization", auth)
		resp, err := client.Do(r)
		if err != nil {
			t.Fatal(err)
		}
		body, err := io.ReadAll(resp.Body)
		resp.Body.Close()
		if err != nil {
			t.Fatal(err)
		}
		expected := 200
		if i == 2 {
			expected = 401
		}
		if i == 3 {
			expected = 400
		}
		if resp.StatusCode != expected {
			t.Fatalf("status %d expected %d", resp.StatusCode, expected)
		}
		if i < 2 && !strings.HasPrefix(string(body), "IDENTITYv1 ") {
			t.Fatal("identity missing")
		}
	}
	if opened.Load() != 1 {
		t.Fatalf("expected one persistent connection, got %d", opened.Load())
	}
	q.ScriptSHA = strings.Repeat("0", 64)
	if _, _, err := m.validate(q); err == nil {
		t.Fatal("foreign program accepted")
	}
}

func TestStatusUsesProcfsWithNoSubprocess(t *testing.T) {
	m := testManager(t)
	// A sealed state is still required to carry the exact reservation/claim.
	token := strings.Repeat("b", 32)
	dir := "/tmp/cityverse-detached-" + token
	if err := os.Mkdir(dir, 0700); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	r := reservation{Token: token, Schema: "cityverse.detached-shard/v1", SelectedRole: "server",
		SelectedUID: m.uid, SelectedBoot: m.boot, SelectedNetns: m.netns,
		Roles: []role{{Role: "server", UID: m.uid, Pod: m.pod, Namespace: m.namespace, Boot: m.boot, Netns: m.netns}}}
	raw, _ := json.Marshal(r)
	for name, value := range map[string]string{"reservation.json": string(raw), "claim": digest(raw) + " " + m.boot + " " + m.netns + "\n", "process": "101 202 101\n", "seal": "sealed"} {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(value), 0600); err != nil {
			t.Fatal(err)
		}
	}
	q := testRequest(m, "status")
	q.Encoded = base64.StdEncoding.EncodeToString(raw)
	// No child command can run with this PATH.
	t.Setenv("PATH", t.TempDir())
	start := time.Now()
	for i := 0; i < 20; i++ {
		_, raw, err := m.validate(q)
		if err != nil {
			t.Fatal(err)
		}
		status, err := m.status(r, raw)
		if err != nil {
			t.Fatal(err)
		}
		if !strings.Contains(status, " SEALED ") {
			t.Fatal("wrong status")
		}
	}
	t.Logf("20 direct status reads: %s", time.Since(start))
	q.PodUID = "foreign"
	if _, _, err := m.validate(q); err == nil {
		t.Fatal("foreign Pod accepted")
	}
	if err := os.WriteFile(dir+"/reservation.json", []byte("changed"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := m.status(r, raw); err == nil {
		t.Fatal("changed state accepted")
	}
}
