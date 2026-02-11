use std::collections::HashMap;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};
use tokio::sync::Mutex;
use tracing::{error, info, warn};

/// VPN instance configuration
struct InstanceConfig {
    control_port: u16,
    region: &'static str,
    name: &'static str,
}

/// VPN reconnect state tracked per-instance in main.rs
#[derive(Clone)]
pub struct VpnReconnectState {
    pub last_reconnect: f64,
    pub attempts: u32,
}

impl Default for VpnReconnectState {
    fn default() -> Self {
        Self {
            last_reconnect: 0.0,
            attempts: 0,
        }
    }
}

const VPN_RECONNECT_COOLDOWN: f64 = 30.0;
const VPN_MAX_RECONNECT_ATTEMPTS: u32 = 3;

/// Manages VPN connections for multiple instances
pub struct VpnManager {
    username: String,
    password: String,
    last_reconnect: Mutex<HashMap<String, f64>>,
    reconnect_cooldown: f64,
    instances: HashMap<String, InstanceConfig>,
}

impl VpnManager {
    pub fn new(username: String, password: String) -> Self {
        let mut instances = HashMap::new();
        instances.insert(
            "instance-sg".to_string(),
            InstanceConfig {
                control_port: 8001,
                region: "singapore",
                name: "Singapore",
            },
        );
        instances.insert(
            "instance-jp".to_string(),
            InstanceConfig {
                control_port: 8002,
                region: "japan",
                name: "Japan",
            },
        );
        instances.insert(
            "instance-us".to_string(),
            InstanceConfig {
                control_port: 8003,
                region: "usa",
                name: "USA",
            },
        );

        Self {
            username,
            password,
            last_reconnect: Mutex::new(HashMap::new()),
            reconnect_cooldown: 30.0,
            instances,
        }
    }

    pub async fn get_instance_status(
        &self,
        instance_id: &str,
    ) -> Option<serde_json::Value> {
        let config = self.instances.get(instance_id)?;
        let client = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(10))
            .build()
            .ok()?;

        let status_resp = client
            .get(format!(
                "http://localhost:{}/v1/vpn/status",
                config.control_port
            ))
            .basic_auth(&self.username, Some(&self.password))
            .send()
            .await
            .ok()?;

        if !status_resp.status().is_success() {
            error!("Failed to get status for {instance_id}: {}", status_resp.status());
            return None;
        }

        let mut status_data: serde_json::Value = status_resp.json().await.ok()?;

        // Get public IP
        if let Ok(ip_resp) = client
            .get(format!(
                "http://localhost:{}/v1/publicip/ip",
                config.control_port
            ))
            .basic_auth(&self.username, Some(&self.password))
            .send()
            .await
        {
            if ip_resp.status().is_success() {
                if let Ok(ip_data) = ip_resp.json::<serde_json::Value>().await {
                    status_data["public_ip"] = ip_data["public_ip"].clone();
                }
            }
        }

        info!("{} status: {status_data}", config.name);
        Some(status_data)
    }

    pub async fn reconnect_vpn(&self, instance_id: &str) -> bool {
        let config = match self.instances.get(instance_id) {
            Some(c) => c,
            None => {
                error!("Unknown instance: {instance_id}");
                return false;
            }
        };

        // Check cooldown
        let now = now_secs();
        {
            let mut last = self.last_reconnect.lock().await;
            let last_time = last.get(instance_id).copied().unwrap_or(0.0);
            if now - last_time < self.reconnect_cooldown {
                warn!("Reconnect cooldown active for {instance_id}, skipping");
                return false;
            }
            last.insert(instance_id.to_string(), now);
        }

        let client = match reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(30))
            .build()
        {
            Ok(c) => c,
            Err(e) => {
                error!("Failed to create HTTP client: {e}");
                return false;
            }
        };

        info!("Triggering VPN reconnect for {} ({instance_id})", config.name);

        // Step 1: Stop VPN
        info!("Stopping VPN for {}...", config.name);
        let stop_result = client
            .put(format!(
                "http://localhost:{}/v1/vpn/status",
                config.control_port
            ))
            .basic_auth(&self.username, Some(&self.password))
            .json(&serde_json::json!({"status": "stopped"}))
            .send()
            .await;

        match stop_result {
            Ok(r) if r.status().is_success() => {}
            Ok(r) => {
                error!("❌ Failed to stop VPN for {}: {}", config.name, r.status());
                return false;
            }
            Err(e) => {
                error!("❌ Error stopping VPN for {}: {e}", config.name);
                return false;
            }
        }

        tokio::time::sleep(std::time::Duration::from_secs(2)).await;

        // Step 2: Start VPN (gets new IP)
        info!("Starting VPN for {}...", config.name);
        let start_result = client
            .put(format!(
                "http://localhost:{}/v1/vpn/status",
                config.control_port
            ))
            .basic_auth(&self.username, Some(&self.password))
            .json(&serde_json::json!({"status": "running"}))
            .send()
            .await;

        match start_result {
            Ok(r) if r.status().is_success() => {
                info!("✅ VPN reconnect triggered for {}", config.name);
                tokio::time::sleep(std::time::Duration::from_secs(5)).await;

                if let Some(status) = self.get_instance_status(instance_id).await {
                    info!(
                        "🔄 {} new IP: {}",
                        config.name,
                        status["public_ip"].as_str().unwrap_or("unknown")
                    );
                }
                true
            }
            Ok(r) => {
                error!("❌ Failed to start VPN for {}: {}", config.name, r.status());
                false
            }
            Err(e) => {
                error!("❌ Error starting VPN for {}: {e}", config.name);
                false
            }
        }
    }

    pub async fn rotate_server(
        &self,
        instance_id: &str,
        new_country: Option<&str>,
    ) -> bool {
        let config = match self.instances.get(instance_id) {
            Some(c) => c,
            None => {
                error!("Unknown instance: {instance_id}");
                return false;
            }
        };

        let target_country = new_country
            .map(|s| s.to_string())
            .unwrap_or_else(|| {
                match config.region {
                    "singapore" => "Japan",
                    "japan" => "USA",
                    "usa" => "Singapore",
                    _ => "Singapore",
                }
                .to_string()
            });

        info!("🌏 Rotating {} to {target_country}", config.name);

        let client = match reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(30))
            .build()
        {
            Ok(c) => c,
            Err(e) => {
                error!("Failed to create HTTP client: {e}");
                return false;
            }
        };

        let result = client
            .put(format!(
                "http://localhost:{}/v1/settings",
                config.control_port
            ))
            .basic_auth(&self.username, Some(&self.password))
            .json(&serde_json::json!({
                "vpn": {
                    "provider": {
                        "name": "mullvad",
                        "server_selection": {
                            "countries": [target_country]
                        }
                    }
                }
            }))
            .send()
            .await;

        match result {
            Ok(r) if r.status().is_success() => {
                info!("✅ Server rotation initiated for {}", config.name);
                self.reconnect_vpn(instance_id).await
            }
            Ok(r) => {
                error!("❌ Failed to rotate server: {}", r.status());
                false
            }
            Err(e) => {
                error!("❌ Error rotating server: {e}");
                false
            }
        }
    }

    pub async fn handle_403_error(&self, instance_id: &str) -> bool {
        warn!("🚨 Handling 403 error for {instance_id}");
        if self.reconnect_vpn(instance_id).await {
            return true;
        }
        info!("🔄 Simple reconnect failed, trying server rotation...");
        self.rotate_server(instance_id, None).await
    }
}

/// Trigger VPN reconnect for the local instance (called from request handlers).
/// Uses per-instance state with cooldown and exponential backoff.
pub async fn trigger_local_vpn_reconnect(
    state: &Arc<Mutex<VpnReconnectState>>,
    instance_id: &str,
    gluetun_port: u16,
    gluetun_user: &str,
    gluetun_pass: &str,
) -> Result<bool, String> {
    let mut st = state.lock().await;
    let now = now_secs();

    if st.attempts >= VPN_MAX_RECONNECT_ATTEMPTS {
        return Err(format!(
            "Max VPN reconnect attempts ({VPN_MAX_RECONNECT_ATTEMPTS}) reached for {instance_id}"
        ));
    }

    if now - st.last_reconnect < VPN_RECONNECT_COOLDOWN {
        info!("VPN reconnect cooldown active for {instance_id}, skipping");
        return Ok(false);
    }

    st.last_reconnect = now;
    st.attempts += 1;
    let attempt = st.attempts;
    drop(st); // release lock before async work

    let backoff = std::cmp::min(5 * (1u64 << (attempt - 1)), 20);

    warn!(
        "🔄 Triggering VPN reconnect for {instance_id} - Attempt {attempt}/{VPN_MAX_RECONNECT_ATTEMPTS}"
    );

    if attempt > 1 {
        info!("🕒 Waiting {backoff}s before reconnect attempt...");
        tokio::time::sleep(std::time::Duration::from_secs(backoff)).await;
    }

    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(30))
        .build()
        .map_err(|e| format!("HTTP client error: {e}"))?;

    let resp = client
        .put(format!("http://localhost:{gluetun_port}/v1/vpn/status"))
        .basic_auth(gluetun_user, Some(gluetun_pass))
        .json(&serde_json::json!({"status": "reconnecting"}))
        .send()
        .await
        .map_err(|e| format!("VPN reconnect request failed: {e}"))?;

    if resp.status().is_success() {
        info!("✅ VPN reconnect triggered successfully for {instance_id} (attempt {attempt}/{VPN_MAX_RECONNECT_ATTEMPTS})");
        let mut st = state.lock().await;
        st.attempts = 0;
        Ok(true)
    } else {
        error!(
            "❌ Failed to trigger VPN reconnect: HTTP {} (attempt {attempt}/{VPN_MAX_RECONNECT_ATTEMPTS})",
            resp.status()
        );
        Ok(false)
    }
}

fn now_secs() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs_f64()
}
