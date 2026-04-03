package delivery

import (
	"bytes"
	"fmt"
	"log"
	"net/http"
	"os/exec"
	"strings"
)

// StreamWorkerMP3 delegates MP3 generation to the Python worker container via docker exec.
func (d *Delivery) StreamWorkerMP3(
	w http.ResponseWriter,
	r *http.Request,
	workerID string,
	plan DeliveryPlan,
	key string,
) bool {
	container := workerContainerName(workerID)
	if container == "" {
		WriteJSON(w, http.StatusBadGateway, map[string]string{"error": "Invalid worker for MP3 stream"})
		return false
	}
	if strings.TrimSpace(key) == "" {
		WriteJSON(w, http.StatusBadGateway, map[string]string{"error": "Missing MP3 session key"})
		return false
	}

	cmd := exec.CommandContext(
		r.Context(),
		"docker", "exec", "-i",
		container,
		"python", "extractor/mp3_stream.py", key,
	)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		WriteJSON(w, http.StatusBadGateway, map[string]string{"error": "Failed to create worker MP3 pipe", "detail": err.Error()})
		return false
	}
	var stderr bytes.Buffer
	cmd.Stderr = &stderr

	if err := cmd.Start(); err != nil {
		WriteJSON(w, http.StatusBadGateway, map[string]string{"error": "Failed to start worker MP3 process", "detail": err.Error()})
		return false
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
	return true
}

func workerContainerName(workerID string) string {
	if !strings.HasPrefix(workerID, "w") {
		return ""
	}
	suffix := strings.TrimPrefix(workerID, "w")
	if suffix == "" {
		return ""
	}
	return fmt.Sprintf("ytdlp-stream-%s", suffix)
}
