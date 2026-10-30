package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"io"
	"log"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// Exercise the actual HTTP port and real Bash child with a local feature-only
// iperf fixture. No UE, network traffic, Pod, or production authority is used.
func capabilityAPISetup(t *testing.T) *manager {
	t.Helper()
	m := testManager(t)
	previous := log.Writer()
	log.SetOutput(io.Discard)
	t.Cleanup(func() { log.SetOutput(previous) })
	dir := t.TempDir()
	feature := "#!/bin/bash\ncase $1 in\n--help) printf '%s' \"$CAP_HELP\"; exit \"${CAP_EXIT:-0}\";;\n--version) printf '%s' \"$CAP_VERSION\";;\n*) exit 2;;\nesac\n"
	if err := os.WriteFile(filepath.Join(dir, "iperf3"), []byte(feature), 0700); err != nil {
		t.Fatal(err)
	}
	// Count the two eliminated external executions without changing their output.
	for _, name := range []string{"base64", "sha256sum"} {
		actual, err := exec.LookPath(name)
		if err != nil {
			t.Fatal(err)
		}
		wrapper := "#!/bin/bash\nprintf x >> \"$CAP_COUNTER\"\nexec '" + actual + "' \"$@\"\n"
		if err := os.WriteFile(filepath.Join(dir, name), []byte(wrapper), 0700); err != nil {
			t.Fatal(err)
		}
	}
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
	t.Setenv("CAP_COUNTER", filepath.Join(dir, "counter"))
	t.Setenv("CAP_HELP", "--gsro --window")
	t.Setenv("CAP_VERSION", "iperf 3.21 GSO/GRO support")
	t.Setenv("CAP_EXIT", "0")
	return m
}

func capabilityRaw(m *manager, selectedRole string) []byte {
	r := reservation{Token: strings.Repeat("c", 32), Schema: "cityverse.detached-shard/v2",
		SelectedRole: selectedRole, SelectedUID: m.uid, SelectedBoot: m.boot, SelectedNetns: m.netns,
		Roles: []role{{Role: selectedRole, UID: m.uid, Pod: m.pod, Namespace: m.namespace, Boot: m.boot, Netns: m.netns}}}
	raw, _ := json.Marshal(r)
	return append(raw[:len(raw)-1], []byte(`,"trafficTuning":{"udpGsro":true,"socketBufferBytes":4194304}}`)...)
}

func capabilityPost(t *testing.T, m *manager, raw []byte, mutate func(*request)) (int, string) {
	t.Helper()
	q := request{Action: "capability", Encoded: base64.StdEncoding.EncodeToString(raw),
		ScriptSHA: m.tunedSHA, Timeout: 2, PodUID: m.uid}
	if mutate != nil {
		mutate(&q)
	}
	body, _ := json.Marshal(q)
	req := httptest.NewRequest("POST", "/v1/control", bytes.NewReader(body))
	req.Header.Set("Authorization", "Bearer "+m.token)
	response := httptest.NewRecorder()
	m.ServeHTTP(response, req)
	return response.Code, response.Body.String()
}

func TestHTTPCapabilityOriginalWorkerParity(t *testing.T) {
	for _, item := range []struct {
		name   string
		role   string
		edit   func([]byte) []byte
		status int
	}{
		{"server", "server", nil, 200}, {"client", "client", nil, 200},
		{"trailing-lf", "client", func(b []byte) []byte { return append(b, '\n', '\n') }, 200},
		{"pretty", "server", func(b []byte) []byte { var out bytes.Buffer; json.Indent(&out, b, "", " "); return out.Bytes() }, 409},
		{"duplicate-token", "server", func(b []byte) []byte { return append([]byte(`{"token":"`+strings.Repeat("d", 32)+`",`), b[1:]...) }, 200},
		{"nested-token", "client", func(b []byte) []byte {
			return append(b[:len(b)-1], []byte(`,"extra":{"token":"`+strings.Repeat("e", 32)+`"}}`)...)
		}, 200},
		{"zero-window", "server", func(b []byte) []byte { return bytes.ReplaceAll(b, []byte(`4194304`), []byte(`0`)) }, 200},
		{"false-gsro", "client", func(b []byte) []byte { return bytes.ReplaceAll(b, []byte(`"udpGsro":true`), []byte(`"udpGsro":false`)) }, 200},
	} {
		t.Run(item.name, func(t *testing.T) {
			m := capabilityAPISetup(t)
			raw := capabilityRaw(m, item.role)
			if item.edit != nil {
				raw = item.edit(raw)
			}
			probe := m.capabilityScript
			m.capabilityScript = ""
			oldStatus, oldBody := capabilityPost(t, m, raw, nil)
			oldCount, err := os.ReadFile(os.Getenv("CAP_COUNTER"))
			expectedCalls := 2
			if item.status != 200 {
				expectedCalls = 1
			}
			if err != nil || len(oldCount) != expectedCalls {
				t.Fatalf("original decode/hash calls %d, err %v", len(oldCount), err)
			}
			m.capabilityScript = probe
			newStatus, newBody := capabilityPost(t, m, raw, nil)
			newCount, _ := os.ReadFile(os.Getenv("CAP_COUNTER"))
			if oldStatus != item.status || newStatus != oldStatus || newBody != oldBody {
				t.Fatalf("HTTP parity status %d/%d expected %d or receipt mismatch", oldStatus, newStatus, item.status)
			}
			if len(newCount) != expectedCalls {
				t.Fatal("short probe repeated decode/hash")
			}
			if len(m.children) != 0 || lockCount(m) != 0 {
				t.Fatal("completed capability retained child/token")
			}
		})
	}
}

func TestHTTPCapabilityFeatureAndFreshIdentityRejectionsKeepParity(t *testing.T) {
	for _, name := range []string{"help-fails", "help-oversize", "version-oversize", "gsro-missing", "version-marker-missing", "window-missing", "socket-missing", "boot-changed", "netns-changed"} {
		t.Run(name, func(t *testing.T) {
			m := capabilityAPISetup(t)
			switch name {
			case "help-fails":
				t.Setenv("CAP_EXIT", "5")
			case "help-oversize":
				t.Setenv("CAP_HELP", strings.Repeat("x", 32769))
			case "version-oversize":
				t.Setenv("CAP_VERSION", strings.Repeat("x", 4097))
			case "gsro-missing":
				t.Setenv("CAP_HELP", "--window")
			case "version-marker-missing":
				t.Setenv("CAP_VERSION", "iperf 3.21")
			case "window-missing":
				t.Setenv("CAP_HELP", "--gsro")
			case "boot-changed":
				m.boot = "00000000-0000-0000-0000-000000000000"
			case "netns-changed":
				m.netns = "net:[1]"
			}
			raw := capabilityRaw(m, "client")
			if name == "socket-missing" {
				raw = bytes.ReplaceAll(raw, []byte(`"socketBufferBytes":4194304`), []byte(`"other":4194304`))
			}
			probe := m.capabilityScript
			m.capabilityScript = ""
			oldStatus, oldBody := capabilityPost(t, m, raw, nil)
			m.capabilityScript = probe
			status, body := capabilityPost(t, m, raw, nil)
			if oldStatus != 409 || status != oldStatus || body != oldBody {
				t.Fatalf("rejection parity %d/%d", oldStatus, status)
			}
		})
	}
}

func TestHTTPCapabilityOriginalRoutingAndValidation(t *testing.T) {
	for _, name := range []string{"uid", "script", "epoch", "role-ambiguous", "namespace", "v2-legacy-script", "legacy-capability", "batch"} {
		t.Run(name, func(t *testing.T) {
			m := capabilityAPISetup(t)
			raw := capabilityRaw(m, "server")
			mutate := func(q *request) {}
			expected := 400
			switch name {
			case "uid":
				mutate = func(q *request) { q.PodUID = "foreign" }
			case "script":
				mutate = func(q *request) { q.ScriptSHA = strings.Repeat("0", 64) }
			case "epoch":
				mutate = func(q *request) { q.Epoch = 1 }
			case "namespace":
				raw = bytes.ReplaceAll(raw, []byte(m.namespace), []byte("foreign"))
			case "role-ambiguous":
				var r map[string]any
				json.Unmarshal(raw, &r)
				roles := r["roles"].([]any)
				r["roles"] = append(roles, roles[0])
				raw, _ = json.Marshal(r)
			case "v2-legacy-script":
				mutate = func(q *request) { q.ScriptSHA = testRequest(m, "capability").ScriptSHA }
			case "legacy-capability":
				raw = bytes.ReplaceAll(raw, []byte("cityverse.detached-shard/v2"), []byte("cityverse.detached-shard/v1"))
				mutate = func(q *request) { q.ScriptSHA = testRequest(m, "capability").ScriptSHA }
				expected = 409
			case "batch":
				mutate = func(q *request) { q.Mode = "batch" }
			}
			status, body := capabilityPost(t, m, raw, mutate)
			if status != expected || strings.Contains(body, m.token) {
				t.Fatalf("routing status %d expected %d", status, expected)
			}
		})
	}
}

func TestHTTPCapabilityCancelledTokenWaiterDoesNotSpawn(t *testing.T) {
	m := capabilityAPISetup(t)
	raw := capabilityRaw(m, "server")
	release, err := m.acquireToken(context.Background(), strings.Repeat("c", 32))
	if err != nil {
		t.Fatal(err)
	}
	defer release()
	status, _ := capabilityPost(t, m, raw, func(q *request) { q.Timeout = .02 })
	if status != 409 || len(m.children) != 0 {
		t.Fatal("expired capability dispatched")
	}
	if _, err := os.Stat(os.Getenv("CAP_COUNTER")); !os.IsNotExist(err) {
		t.Fatal("expired request invoked worker")
	}
}
