mod config;
mod formats;
mod handlers;
mod ratelimit;
mod response;
mod signature;
mod ytdlp;

use config::Settings;
use ratelimit::RateLimiter;

use tower_http::cors::{Any, CorsLayer};
use tracing::info;

// ============= Application State =============

#[derive(Clone)]
pub struct AppState {
    pub settings: Settings,
    pub rate_limiter: RateLimiter,
}

// ============= Main =============

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt::init();

    let settings = Settings::from_env();

    info!("Starting serveryt-rs on port {}", settings.port);
    info!("Base URL: {}", settings.base_url);
    info!("Signature TTL: {}s", settings.signature_ttl);
    info!(
        "Rate limit: {} requests per {}s",
        settings.rate_limit_requests, settings.rate_limit_window
    );
    if let Some(ref cp) = settings.cookies_path {
        info!("Cookies: {:?}", cp);
    }

    // Initialize rate limiter
    let rate_limiter = RateLimiter::new(
        settings.rate_limit_requests,
        settings.rate_limit_window,
    );

    let state = AppState {
        settings: settings.clone(),
        rate_limiter,
    };

    // CORS
    let cors = CorsLayer::new()
        .allow_origin(Any)
        .allow_methods([
            axum::http::Method::GET,
            axum::http::Method::POST,
            axum::http::Method::OPTIONS,
        ])
        .allow_headers(Any)
        .expose_headers([
            "Content-Disposition".parse().unwrap(),
            "Content-Length".parse().unwrap(),
        ]);

    // Router
    let app = axum::Router::new()
        .route("/", axum::routing::get(handlers::root))
        .route("/health", axum::routing::get(handlers::health))
        .route("/download", axum::routing::post(handlers::download))
        .route("/stream", axum::routing::get(handlers::stream))
        .layer(cors)
        .with_state(state);

    let addr = format!("0.0.0.0:{}", settings.port);
    info!("🚀 serveryt-rs listening on {addr}");
    info!("   Runtime: Tokio + PyO3 (yt-dlp) - Stateless");
    info!("   Endpoints: /download, /stream, /health");

    let listener = tokio::net::TcpListener::bind(&addr).await.unwrap();
    // Use into_make_service_with_connect_info to get client IP for rate limiting
    axum::serve(
        listener,
        app.into_make_service_with_connect_info::<std::net::SocketAddr>(),
    )
    .await
    .unwrap();
}
