package sbi

import (
	"bytes"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	pcfcontext "github.com/free5gc/pcf/internal/context"
	"github.com/free5gc/pcf/internal/sbi/processor"
	"github.com/gin-gonic/gin"
)

// Embed unused app methods, but execute the real OAM handler and processor.
// The exercised processor uses only the in-memory UePool; no NF is started.
type corsRegressionPCF struct{ pcf }

func (corsRegressionPCF) Processor() *processor.Processor { return &processor.Processor{} }

func corsRegressionRouter(recovery io.Writer) *gin.Engine {
	router := gin.New()
	router.Use(gin.RecoveryWithWriter(recovery))
	server := &Server{pcf: corsRegressionPCF{}, router: router}
	applyRoutes(router.Group("/oam"), server.getOamRoutes())
	router.GET("/missing-supi", server.HTTPOAMGetAmPolicy)
	return router
}

func corsRegressionRequest(router *gin.Engine, path string) *httptest.ResponseRecorder {
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, path, nil)
	request.Header.Set("Origin", "https://example.invalid")
	router.ServeHTTP(recorder, request)
	return recorder
}

func corsRegressionHeaders(recorder *httptest.ResponseRecorder) error {
	want := map[string]string{
		"Access-Control-Allow-Origin":      "*",
		"Access-Control-Allow-Credentials": "true",
		"Access-Control-Allow-Headers":     "Content-Type, Content-Length, Accept-Encoding, X-CSRF-Token, Authorization, accept, origin, Cache-Control, X-Requested-With",
		"Access-Control-Allow-Methods":     "POST, OPTIONS, GET, PUT, PATCH, DELETE",
	}
	for key, value := range want {
		if got := recorder.Header().Get(key); got != value {
			return fmt.Errorf("header %s = %q; want %q", key, got, value)
		}
	}
	return nil
}

func TestOAMCorsSequential(t *testing.T) {
	gin.SetMode(gin.TestMode)
	var recovery bytes.Buffer
	router := corsRegressionRouter(&recovery)
	before := len(router.Handlers)
	firstBad, failures := -1, 0
	for i := 0; i < 256; i++ {
		r := corsRegressionRequest(router, "/oam/am-policy/imsi-cors-regression-absent")
		if r.Code != http.StatusNotFound || !strings.Contains(r.Body.String(), "CONTEXT_NOT_FOUND") || corsRegressionHeaders(r) != nil {
			failures++
			if firstBad < 0 {
				firstBad = i + 1
			}
		}
	}
	t.Logf("requests=256 firstBad=%d failures=%d handlersBefore=%d handlersAfter=%d tooManyHandlers=%t",
		firstBad, failures, before, len(router.Handlers), strings.Contains(recovery.String(), "too many handlers"))
	if failures != 0 || len(router.Handlers) != before {
		t.Fatalf("OAM request mutated router or failed: failures=%d handlers=%d->%d", failures, before, len(router.Handlers))
	}
}

func TestOAMCorsConcurrent(t *testing.T) {
	gin.SetMode(gin.TestMode)
	router := corsRegressionRouter(io.Discard)
	before := len(router.Handlers)
	errors := make(chan error, 256)
	var workers sync.WaitGroup
	for worker := 0; worker < 16; worker++ {
		workers.Add(1)
		go func() {
			defer workers.Done()
			for request := 0; request < 16; request++ {
				r := corsRegressionRequest(router, "/oam/am-policy/imsi-cors-regression-absent")
				if r.Code != http.StatusNotFound || !strings.Contains(r.Body.String(), "CONTEXT_NOT_FOUND") {
					errors <- fmt.Errorf("unexpected OAM status/body: %d %s", r.Code, r.Body.String())
				} else if err := corsRegressionHeaders(r); err != nil {
					errors <- err
				}
			}
		}()
	}
	workers.Wait()
	close(errors)
	for err := range errors {
		t.Error(err)
	}
	if len(router.Handlers) != before {
		t.Errorf("handler count changed %d->%d", before, len(router.Handlers))
	}
	t.Logf("concurrent requests=256 workers=16 handlers=%d->%d", before, len(router.Handlers))
}

func TestOAMCorsStatusAndHeaders(t *testing.T) {
	gin.SetMode(gin.TestMode)
	router := corsRegressionRouter(io.Discard)
	supi := "imsi-cors-regression-present"
	pcfcontext.GetSelf().UePool.Store(supi, &pcfcontext.UeContext{})
	defer pcfcontext.GetSelf().UePool.Delete(supi)
	for _, tc := range []struct {
		path   string
		status int
		body   string
	}{
		{"/missing-supi", http.StatusBadRequest, "400"},
		{"/oam/am-policy/" + supi, http.StatusOK, "[]"},
		{"/oam/am-policy/imsi-cors-regression-absent", http.StatusNotFound, "CONTEXT_NOT_FOUND"},
	} {
		r := corsRegressionRequest(router, tc.path)
		if r.Code != tc.status || !strings.Contains(r.Body.String(), tc.body) {
			t.Errorf("%s: %d %s", tc.path, r.Code, r.Body.String())
		}
		if err := corsRegressionHeaders(r); err != nil {
			t.Error(err)
		}
	}
	r := corsRegressionRequest(router, "/not-a-route")
	if r.Code != http.StatusNotFound || r.Body.String() != "404 page not found" {
		t.Errorf("router 404 changed: %d %s", r.Code, r.Body.String())
	}
}
