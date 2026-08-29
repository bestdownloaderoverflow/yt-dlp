package main

import (
	"net/url"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

// --- app API shapes (only the fields this service reads) ---------------------

type urlList struct {
	URLList []string `json:"url_list"`
	Width   int      `json:"width"`
	Height  int      `json:"height"`
	URLKey  string   `json:"url_key"`
}

func (u urlList) first() string {
	for _, v := range u.URLList {
		if v != "" {
			return v
		}
	}
	return ""
}

type bitRate struct {
	GearName    string  `json:"gear_name"`
	QualityType int     `json:"quality_type"`
	BitRate     int     `json:"bit_rate"`
	IsByteVC1   int     `json:"is_bytevc1"`
	PlayAddr    urlList `json:"play_addr"`
}

// isHEVC reports whether this rendition is H.265. TikTok calls it bytevc1 and
// flags it three different ways depending on the post, so all three are read.
func (b bitRate) isHEVC() bool {
	return b.IsByteVC1 != 0 ||
		strings.Contains(b.PlayAddr.URLKey, "bytevc1") ||
		strings.Contains(b.PlayAddr.first(), "/media-video-hvc1/")
}

// isByteVC2 marks the newest codec, which almost nothing outside the TikTok app
// decodes. Dropped outright rather than ranked last, matching 9111.
func (b bitRate) isByteVC2() bool {
	return strings.Contains(b.PlayAddr.URLKey, "bytevc2") ||
		strings.Contains(strings.ToLower(b.GearName), "bytevc2")
}

type Aweme struct {
	AwemeID    string `json:"aweme_id"`
	Desc       string `json:"desc"`
	CreateTime int64  `json:"create_time"`
	Author     struct {
		UID          string  `json:"uid"`
		UniqueID     string  `json:"unique_id"`
		Nickname     string  `json:"nickname"`
		Signature    string  `json:"signature"`
		Region       string  `json:"region"`
		AvatarThumb  urlList `json:"avatar_thumb"`
		AvatarMedium urlList `json:"avatar_medium"`
		Avatar168    urlList `json:"avatar_168x168"`
		Avatar300    urlList `json:"avatar_300x300"`
	} `json:"author"`
	Statistics struct {
		CommentCount int `json:"comment_count"`
		DiggCount    int `json:"digg_count"`
		PlayCount    int `json:"play_count"`
		ShareCount   int `json:"share_count"`
	} `json:"statistics"`
	Video struct {
		// Milliseconds here, unlike the web SSR payload which reports seconds.
		Duration     int       `json:"duration"`
		PlayAddr     urlList   `json:"play_addr"`
		PlayAddrH264 urlList   `json:"play_addr_h264"`
		IsByteVC1    int       `json:"is_bytevc1"`
		DownloadAddr urlList   `json:"download_addr"`
		Cover        urlList   `json:"cover"`
		OriginCover  urlList   `json:"origin_cover"`
		DynamicCover urlList   `json:"dynamic_cover"`
		BitRate      []bitRate `json:"bit_rate"`
	} `json:"video"`
	Music struct {
		Title    string  `json:"title"`
		Author   string  `json:"author"`
		Duration int     `json:"duration"` // seconds
		PlayURL  urlList `json:"play_url"`
	} `json:"music"`
	ImagePostInfo *struct {
		Images []struct {
			DisplayImage urlList `json:"display_image"`
		} `json:"images"`
	} `json:"image_post_info"`
}

// --- response shapes (byte-for-byte the contract 9111 already serves) --------

type Statistics struct {
	PlayCount    int `json:"play_count"`
	DiggCount    int `json:"digg_count"`
	CommentCount int `json:"comment_count"`
	ShareCount   int `json:"share_count"`
}

type Author struct {
	Nickname     string `json:"nickname"`
	UniqueID     string `json:"uniqueId"`
	Signature    string `json:"signature"`
	Avatar       string `json:"avatar"`
	AvatarThumb  string `json:"avatarThumb"`
	AvatarMedium string `json:"avatarMedium"`
	AvatarLarger string `json:"avatarLarger"`
}

type Response struct {
	Status        string         `json:"status"`
	ExtractSource string         `json:"extract_source"`
	Title         string         `json:"title"`
	Description   string         `json:"description"`
	Statistics    Statistics     `json:"statistics"`
	Artist        string         `json:"artist"`
	Cover         string         `json:"cover"`
	DynamicCover  string         `json:"dynamic_cover,omitempty"`
	Duration      int            `json:"duration"`
	Audio         string         `json:"audio"`
	DownloadLink  map[string]any `json:"download_link"`
	MusicDuration int            `json:"music_duration,omitempty"`
	Photos        []Photo        `json:"photos,omitempty"`
	Slideshow     string         `json:"download_slideshow,omitempty"`
	SlideshowLink string         `json:"download_slideshow_link,omitempty"`
	Author        Author         `json:"author"`
}

type Photo struct {
	Type         string `json:"type"`
	URL          string `json:"url"`
	DownloadLink string `json:"download_link"`
}

const extractSource = "app_api"

var unsafeFilename = regexp.MustCompile(`[^a-zA-Z0-9_-]+`)

func sanitizeFilenamePart(v, fallback string) string {
	cleaned := strings.Trim(unsafeFilename.ReplaceAllString(v, "_"), "_")
	if cleaned == "" {
		return fallback
	}
	return cleaned
}

// mediaIdentity keys a media object by its CDN path so the same object served
// from v16/v19 mirrors is not advertised twice as different qualities.
func mediaIdentity(raw string) string {
	if raw == "" {
		return ""
	}
	u, err := url.Parse(raw)
	if err != nil {
		return raw
	}
	return u.Path
}

// gearResolution reads the vertical resolution out of a gear name
// (adapt_lower_720_1, adapt_540_1, lowest_540_1 ...), falling back to the
// dimensions the CDN reports for the rendition.
func gearResolution(gear string, fallbackHeight int) int {
	gear = strings.ToLower(gear)
	for _, p := range []struct {
		token string
		value int
	}{{"2160", 2160}, {"1440", 1440}, {"1080", 1080}, {"720", 720}, {"540", 540}, {"480", 480}, {"360", 360}} {
		if strings.Contains(gear, p.token) {
			return p.value
		}
	}
	return fallbackHeight
}

// bestHD picks the biggest rendition whose codec will actually play, falling
// back to the biggest rendition of any codec when none of them will.
//
// Two things make this harder than sorting on resolution. The app API leaves
// codec_type null, so the web path's codec filter does not work here -- the real
// marker is is_bytevc1, TikTok's name for H.265. And the H.264 renditions are
// not all in bit_rate: play_addr_h264 sits beside it and is often the only H.264
// copy on offer, so a search that reads bit_rate alone misses it and concludes
// the post has no playable rendition at all.
//
// Ordering is therefore playable-codec first, then resolution, then bitrate.
// H.265 still wins when it is the only thing there -- a large file the user may
// need a codec pack for beats no HD link at all -- and no_watermark stays H.264
// in that case, so there is always one link that opens anywhere.
func bestHD(a *Aweme) (string, string) {
	type scored struct {
		url      string
		identity string
		h264     bool
		res      int
		bitrate  int
	}
	var out []scored
	add := func(u string, h264 bool, res, bitrate int) {
		if u == "" {
			return
		}
		out = append(out, scored{u, mediaIdentity(u), h264, res, bitrate})
	}
	for _, r := range a.Video.BitRate {
		if r.isByteVC2() {
			continue
		}
		add(r.PlayAddr.first(), !r.isHEVC(),
			gearResolution(r.GearName, r.PlayAddr.Height), r.BitRate)
	}
	add(a.Video.PlayAddrH264.first(), true, a.Video.PlayAddrH264.Height, 0)
	add(a.Video.PlayAddr.first(), a.Video.IsByteVC1 == 0, a.Video.PlayAddr.Height, 0)
	if len(out) == 0 {
		return "", ""
	}
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].h264 != out[j].h264 {
			return out[i].h264
		}
		if out[i].res != out[j].res {
			return out[i].res > out[j].res
		}
		return out[i].bitrate > out[j].bitrate
	})
	return out[0].url, out[0].identity
}

func buildAuthor(a *Aweme) Author {
	nickname := a.Author.Nickname
	if nickname == "" {
		nickname = a.Author.UniqueID
	}
	if nickname == "" {
		nickname = "unknown"
	}
	uniqueID := a.Author.UniqueID
	if uniqueID == "" {
		uniqueID = nickname
	}
	// The app API has no avatar_larger. Its sizes are avatar_thumb 100x100,
	// avatar_300x300, and avatar_medium at 720x720 -- so "medium" is actually
	// the largest one offered. Map by real size, not by name, or avatarLarger
	// ends up smaller than avatarMedium.
	larger := a.Author.AvatarMedium.first()
	medium := a.Author.Avatar300.first()
	thumb := a.Author.AvatarThumb.first()
	if medium == "" {
		medium = a.Author.Avatar168.first()
	}
	if larger == "" {
		larger = medium
	}
	if medium == "" {
		medium = larger
	}
	if thumb == "" {
		thumb = medium
	}
	return Author{
		Nickname:     nickname,
		UniqueID:     uniqueID,
		Signature:    a.Author.Signature,
		Avatar:       larger,
		AvatarThumb:  thumb,
		AvatarMedium: medium,
		AvatarLarger: larger,
	}
}

// Build converts one app API post into the response 9111 already returns.
func (s *Server) Build(a *Aweme, canonicalURL string) *Response {
	author := buildAuthor(a)
	safeAuthor := sanitizeFilenamePart(author.Nickname, "tiktok")
	stats := Statistics{
		PlayCount:    a.Statistics.PlayCount,
		DiggCount:    a.Statistics.DiggCount,
		CommentCount: a.Statistics.CommentCount,
		ShareCount:   a.Statistics.ShareCount,
	}
	audio := a.Music.PlayURL.first()

	base := &Response{
		ExtractSource: extractSource,
		Title:         a.Desc,
		Description:   a.Desc,
		Statistics:    stats,
		Artist:        author.Nickname,
		Audio:         audio,
		Author:        author,
		DownloadLink:  map[string]any{},
	}

	// A photo post still carries a video object (with duration 0), so the image
	// payload has to be what decides the shape.
	if a.ImagePostInfo != nil && len(a.ImagePostInfo.Images) > 0 {
		var images []string
		seen := map[string]bool{}
		for _, img := range a.ImagePostInfo.Images {
			u := img.DisplayImage.first()
			if u == "" || seen[u] {
				continue
			}
			seen[u] = true
			images = append(images, u)
		}
		if len(images) > 0 {
			return s.buildPicker(base, a, canonicalURL, safeAuthor, images, audio)
		}
	}
	return s.buildVideo(base, a, canonicalURL, safeAuthor, audio)
}

func (s *Server) buildPicker(r *Response, a *Aweme, canonicalURL, safeAuthor string,
	images []string, audio string) *Response {

	var links []string
	for i, img := range images {
		key := s.sessions.Create(SessionData{
			URL:        canonicalURL,
			Type:       "photo",
			PhotoIndex: i + 1,
			DirectURL:  img,
			Author:     safeAuthor,
		})
		link := downloadPath(key)
		links = append(links, link)
		r.Photos = append(r.Photos, Photo{Type: "photo", URL: img, DownloadLink: link})
	}
	r.Status = "picker"
	r.Cover = images[0]
	r.Duration = len(images) * s.cfg.SlideshowSecondsPerImage
	r.DownloadLink["no_watermark"] = links

	if audio != "" {
		key := s.sessions.Create(SessionData{
			URL:       canonicalURL,
			Type:      "mp3",
			DirectURL: audio,
			Author:    safeAuthor,
			Duration:  a.Music.Duration,
		})
		r.DownloadLink["mp3"] = downloadPath(key)
	}

	slideshowKey := s.sessions.Create(SessionData{
		URL:       canonicalURL,
		Type:      "slideshow",
		PhotoURLs: images,
		AudioURL:  audio,
		Author:    safeAuthor,
	})
	r.Slideshow = downloadPath(slideshowKey)
	r.SlideshowLink = r.Slideshow
	return r
}

func (s *Server) buildVideo(r *Response, a *Aweme, canonicalURL, safeAuthor string,
	audio string) *Response {

	// play_addr_h264 is the guaranteed H.264 twin of play_addr; when the post is
	// published as H.265, play_addr itself is the one that will not play.
	play := a.Video.PlayAddrH264.first()
	if play == "" {
		play = a.Video.PlayAddr.first()
	}
	download := a.Video.DownloadAddr.first()
	hd, hdIdentity := bestHD(a)
	if play == "" && hd == "" && download == "" {
		return nil
	}

	// The app API reports duration in milliseconds; the contract is seconds.
	duration := a.Video.Duration / 1000
	r.Status = "tunnel"
	r.Duration = duration
	r.Cover = a.Video.Cover.first()
	if r.Cover == "" {
		r.Cover = a.Video.OriginCover.first()
	}
	r.DynamicCover = a.Video.DynamicCover.first()
	r.MusicDuration = a.Music.Duration
	if r.MusicDuration == 0 {
		r.MusicDuration = duration
	}

	playIdentity := mediaIdentity(play)
	if play != "" {
		key := s.sessions.Create(SessionData{
			URL: canonicalURL, Type: "video", Quality: "no_watermark",
			DirectURL: play, Author: safeAuthor, Duration: duration,
		})
		r.DownloadLink["no_watermark"] = downloadPath(key)
	}
	if hd != "" && (play == "" || hdIdentity != playIdentity) {
		key := s.sessions.Create(SessionData{
			URL: canonicalURL, Type: "video", Quality: "no_watermark_hd",
			DirectURL: hd, Author: safeAuthor, Duration: duration,
		})
		r.DownloadLink["no_watermark_hd"] = downloadPath(key)
		if _, ok := r.DownloadLink["no_watermark"]; !ok {
			r.DownloadLink["no_watermark"] = r.DownloadLink["no_watermark_hd"]
		}
	}
	// download_addr is the watermarked rendition. Only advertise it when it is
	// genuinely a different object, not the same file behind a mirror host.
	downloadIdentity := mediaIdentity(download)
	if download != "" && downloadIdentity != playIdentity && downloadIdentity != hdIdentity {
		key := s.sessions.Create(SessionData{
			URL: canonicalURL, Type: "video", Quality: "watermark",
			DirectURL: download, Author: safeAuthor, Duration: duration,
		})
		r.DownloadLink["watermark"] = downloadPath(key)
	}
	if audio != "" {
		musicDuration := a.Music.Duration
		if musicDuration == 0 {
			musicDuration = duration
		}
		key := s.sessions.Create(SessionData{
			URL: canonicalURL, Type: "mp3", DirectURL: audio,
			Author: safeAuthor, Duration: musicDuration,
		})
		r.DownloadLink["mp3"] = downloadPath(key)
	}
	return r
}

func downloadPath(key string) string {
	return "/tiktok/download?key=" + url.QueryEscape(key)
}

// BuildFilename mirrors 9111 so a saved file keeps the same name on either service.
func BuildFilename(s SessionData) string {
	author := s.Author
	if author == "" {
		author = "tiktok"
	}
	switch s.Type {
	case "photo":
		idx := s.PhotoIndex
		if idx == 0 {
			idx = 1
		}
		return author + "_photo_" + strconv.Itoa(idx) + ".jpeg"
	case "mp3":
		return author + "_mp3.mp3"
	case "slideshow", "slideshow_render":
		return author + "_slideshow.mp4"
	}
	if s.Quality != "" {
		return author + "_video_" + s.Quality + ".mp4"
	}
	return author + "_video.mp4"
}
