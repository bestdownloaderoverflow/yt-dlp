package main

import (
	"testing"
	"time"
)

func TestClientFetch(t *testing.T) {
	cfg := LoadConfig()
	metrics := NewMetrics()
	client, err := NewClient(cfg, metrics)
	if err != nil {
		t.Fatalf("failed to create client: %v", err)
	}

	awemeID := "7669136311118220577"
	canonical := "https://www.tiktok.com/@raph_m_74/video/" + awemeID

	start := time.Now()
	item, host, err := client.Fetch(awemeID)
	if err != nil {
		t.Fatalf("Fetch failed: %v", err)
	}

	srv := &Server{
		cfg:      cfg,
		client:   client,
		sessions: NewStore(cfg),
		metrics:  metrics,
	}

	res := srv.Build(item, canonical)
	if res == nil {
		t.Fatalf("Build returned nil")
	}

	t.Logf("SUCCESS in %v via host %s", time.Since(start).Round(time.Millisecond), host)
	t.Logf("Title: %s", res.Title)
	t.Logf("Author: %s (@%s)", res.Author.Nickname, res.Author.UniqueID)
}
