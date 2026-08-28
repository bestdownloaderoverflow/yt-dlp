package main

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"log"
	"sync"
	"time"

	"github.com/redis/go-redis/v9"
)

// SessionData is the download ticket handed to the client as an opaque key.
// The field names match the 9111 contract so a session written by either
// service is readable by either service, should they ever share a store.
type SessionData struct {
	URL         string   `json:"url,omitempty"`
	Type        string   `json:"type"`
	Quality     string   `json:"quality,omitempty"`
	PhotoIndex  int      `json:"photo_index,omitempty"`
	DirectURL   string   `json:"direct_url,omitempty"`
	Author      string   `json:"author,omitempty"`
	Duration    int      `json:"duration,omitempty"`
	PhotoURLs   []string `json:"photo_urls,omitempty"`
	AudioURL    string   `json:"audio_url,omitempty"`
	Proxy       string   `json:"proxy,omitempty"`
	Impersonate string   `json:"impersonate,omitempty"`
}

type memoryEntry struct {
	data    []byte
	expires time.Time
}

// Store keeps sessions and the extraction cache in Redis, falling back to
// process memory so a Redis outage degrades throughput instead of the service.
type Store struct {
	rdb        *redis.Client
	sessionTTL time.Duration
	cacheTTL   time.Duration

	mu  sync.Mutex
	mem map[string]memoryEntry
}

func NewStore(cfg Config) *Store {
	s := &Store{
		sessionTTL: cfg.SessionTTL,
		cacheTTL:   cfg.CacheTTL,
		mem:        map[string]memoryEntry{},
	}
	if cfg.RedisURL != "" {
		opt, err := redis.ParseURL(cfg.RedisURL)
		if err != nil {
			log.Printf("[store] REDIS_URL unusable (%v); using in-memory sessions", err)
			return s
		}
		s.rdb = redis.NewClient(opt)
	}
	return s
}

func (s *Store) ctx() (context.Context, context.CancelFunc) {
	return context.WithTimeout(context.Background(), 3*time.Second)
}

func newKey() string {
	buf := make([]byte, 18)
	if _, err := rand.Read(buf); err != nil {
		// Falling back to a timestamp would make keys guessable; refuse instead.
		panic("session key generation failed: " + err.Error())
	}
	return base64.RawURLEncoding.EncodeToString(buf)
}

func (s *Store) put(key string, value []byte, ttl time.Duration) {
	if s.rdb != nil {
		ctx, cancel := s.ctx()
		defer cancel()
		if err := s.rdb.Set(ctx, key, value, ttl).Err(); err == nil {
			return
		}
		log.Printf("[store] redis write failed for %s; using memory", key)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.sweepLocked()
	s.mem[key] = memoryEntry{data: value, expires: time.Now().Add(ttl)}
}

func (s *Store) get(key string) ([]byte, bool) {
	if s.rdb != nil {
		ctx, cancel := s.ctx()
		defer cancel()
		if v, err := s.rdb.Get(ctx, key).Bytes(); err == nil {
			return v, true
		} else if err != redis.Nil {
			log.Printf("[store] redis read failed for %s; falling back to memory", key)
		}
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	e, ok := s.mem[key]
	if !ok || time.Now().After(e.expires) {
		delete(s.mem, key)
		return nil, false
	}
	return e.data, true
}

func (s *Store) sweepLocked() {
	if len(s.mem) < 512 {
		return
	}
	now := time.Now()
	for k, v := range s.mem {
		if now.After(v.expires) {
			delete(s.mem, k)
		}
	}
}

func (s *Store) Create(data SessionData) string {
	key := newKey()
	blob, err := json.Marshal(data)
	if err != nil {
		log.Printf("[store] cannot encode session: %v", err)
		return key
	}
	s.put("session:"+key, blob, s.sessionTTL)
	return key
}

func (s *Store) Get(key string) (SessionData, bool) {
	var out SessionData
	blob, ok := s.get("session:" + key)
	if !ok {
		return out, false
	}
	if err := json.Unmarshal(blob, &out); err != nil {
		return out, false
	}
	return out, true
}

func cacheKey(url string) string {
	sum := sha256.Sum256([]byte(url))
	return "extract:" + hex.EncodeToString(sum[:])[:32]
}

func (s *Store) GetCached(url string) ([]byte, bool) {
	if s.cacheTTL <= 0 {
		return nil, false
	}
	return s.get(cacheKey(url))
}

func (s *Store) SetCached(url string, payload []byte) {
	if s.cacheTTL <= 0 {
		return
	}
	s.put(cacheKey(url), payload, s.cacheTTL)
}
