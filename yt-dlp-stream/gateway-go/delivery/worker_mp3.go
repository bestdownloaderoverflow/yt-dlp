package delivery

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/exec"
	"strconv"
	"strings"
)

// StreamWorkerMP3 delegates MP3 generation to the Python worker container via docker exec.
func (d *Delivery) StreamWorkerMP3(
	w http.ResponseWriter,
	r *http.Request,
	workerID string,
	plan DeliveryPlan,
	key string,
) error {
	container, err := resolveWorkerContainer(r.Context(), workerID)
	if err != nil {
		return err
	}
	if container == "" {
		return fmt.Errorf("unable to resolve worker container for %s", workerID)
	}
	if strings.TrimSpace(key) == "" {
		return errors.New("missing MP3 session key")
	}

	cmd := exec.CommandContext(
		r.Context(),
		"docker", "exec", "-i",
		container,
		"python", "extractor/mp3_stream.py", key,
	)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return fmt.Errorf("create worker MP3 pipe failed: %w", err)
	}
	var stderr bytes.Buffer
	cmd.Stderr = &stderr

	if err := cmd.Start(); err != nil {
		return fmt.Errorf("start worker MP3 process failed (%s): %w", container, err)
	}

	for k, v := range plan.ResponseHeaders {
		w.Header().Set(k, v)
	}
	if w.Header().Get("Content-Type") == "" {
		w.Header().Set("Content-Type", "audio/mpeg")
	}
	w.WriteHeader(http.StatusOK)

	_, copyErr := copyBuffer(w, stdout)
	waitErr := cmd.Wait()
	if copyErr != nil && !isClientAbortError(copyErr) {
		log.Printf("worker mp3 copy error: %v", copyErr)
	}
	if waitErr != nil && r.Context().Err() == nil && !isClientAbortError(waitErr) {
		log.Printf("worker mp3 error: %v, out=%s", waitErr, strings.TrimSpace(stderr.String()))
	}
	return nil
}

func resolveWorkerContainer(ctx context.Context, workerID string) (string, error) {
	service := workerServiceName(workerID)
	if service == "" {
		return "", fmt.Errorf("invalid worker id: %s", workerID)
	}
	if name, ok := findContainerByComposeService(ctx, service); ok {
		return name, nil
	}
	suffix := strings.TrimPrefix(workerID, "w")
	for _, name := range workerContainerAliases(suffix) {
		if containerExists(ctx, name) {
			return name, nil
		}
	}
	return "", fmt.Errorf("container not found for worker %s (service %s)", workerID, service)
}

func workerServiceName(workerID string) string {
	if !strings.HasPrefix(workerID, "w") {
		return ""
	}
	suffix := strings.TrimPrefix(workerID, "w")
	if suffix == "" {
		return ""
	}
	if _, err := strconv.Atoi(suffix); err != nil {
		return ""
	}
	prefix := strings.TrimSpace(os.Getenv("WORKER_SERVICE_PREFIX"))
	if prefix == "" {
		prefix = "ytdlp-"
	}
	return prefix + suffix
}

func workerContainerAliases(workerSuffix string) []string {
	prefixes := []string{"ytdlp-stream-", "ytdlp-"}
	raw := strings.TrimSpace(os.Getenv("WORKER_CONTAINER_PREFIXES"))
	if raw != "" {
		prefixes = prefixes[:0]
		for _, part := range strings.Split(raw, ",") {
			p := strings.TrimSpace(part)
			if p != "" {
				prefixes = append(prefixes, p)
			}
		}
		if len(prefixes) == 0 {
			prefixes = []string{"ytdlp-stream-", "ytdlp-"}
		}
	}
	out := make([]string, 0, len(prefixes))
	for _, p := range prefixes {
		out = append(out, p+workerSuffix)
	}
	return out
}

func findContainerByComposeService(ctx context.Context, service string) (string, bool) {
	cmd := exec.CommandContext(
		ctx,
		"docker", "ps",
		"--filter", "status=running",
		"--filter", "label=com.docker.compose.service="+service,
		"--format", "{{.Names}}",
	)
	out, err := cmd.Output()
	if err != nil {
		return "", false
	}
	name := strings.TrimSpace(string(out))
	if name == "" {
		return "", false
	}
	lines := strings.Split(name, "\n")
	if len(lines) == 0 {
		return "", false
	}
	return strings.TrimSpace(lines[0]), true
}

func containerExists(ctx context.Context, container string) bool {
	cmd := exec.CommandContext(
		ctx,
		"docker", "ps",
		"--filter", "status=running",
		"--filter", "name=^"+container+"$",
		"--format", "{{.Names}}",
	)
	out, err := cmd.Output()
	if err != nil {
		return false
	}
	return strings.TrimSpace(string(out)) != ""
}
