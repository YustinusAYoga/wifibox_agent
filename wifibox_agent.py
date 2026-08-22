import time
import subprocess
import os
import re
import shutil
import requests
import json
import logging
from prometheus_client import start_http_server, Gauge, Info, CollectorRegistry, write_to_textfile

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Configuration ---
PORT = 9101
UPDATE_INTERVAL = 300
TEXT_FILE_PATH = "/home/oldendome/wifibox-agent/meteric-data.txt"
IS_AP_FLAG_FILE = "/home/oldendome/wifibox_is_ap.txt"
LEASE_FILE_DHCLIENT = "/var/lib/dhcp/dhclient.leases"
ID_FILE_DIR = "/home/oldendome/wifibox-agent/data"  
PUSHGATEWAY_URL = "" 

# --- Head Office API Configuration ---
HEAD_OFFICE_API_BASE_URL = "http://100.105.90.66:9102"

registry = CollectorRegistry()

# Existing Metrics
m_internet_up = Gauge('wifibox_internet_up', '1 if wlan0 or eth1 has internet', registry=registry)
m_vpn_up = Gauge('wifibox_vpn_up', '1 if wg5 is alive with recent handshake', registry=registry)
m_vpn_handshake_age = Gauge('wifibox_vpn_handshake_age_seconds', 'Age of VPN handshake in seconds', registry=registry)
m_tailscale_up = Gauge('wifibox_tailscale_up', '1 if tailscale is active and connected', registry=registry)
m_ap_interface_up = Gauge('wifibox_ap_interface_up', '1 if eth0 is connected', registry=registry)
m_ip_forward = Gauge('wifibox_ip_forward_enabled', '1 if ip_forward is enabled', registry=registry)
m_nat_masq = Gauge('wifibox_nat_masquerade_ok', '1 if NAT masquerade is active', registry=registry)
m_connected_devices = Gauge('wifibox_connected_devices', 'Number of connected Domes', registry=registry)
m_dhcp_up = Gauge('wifibox_dhcp_service_up', '1 if dnsmasq or iscdhcp-server is alive', registry=registry)
m_dhcp_backend = Info('wifibox_dhcp_backend', 'DHCP backend in use', registry=registry)
m_dhcp_range_ok = Gauge('wifibox_dhcp_range_ok', '1 if DHCP range config is active', registry=registry)
m_dhcp_leases = Gauge('wifibox_dhcp_active_leases', 'Number of active DHCP leases', registry=registry)
m_dhcp_file_present = Gauge('wifibox_dhcp_lease_file_present', '1 if lease file exists', registry=registry)
m_wg_systemd_up = Gauge('wifibox_wg_systemd_up', '1 if wg-quick@wg5 is active', registry=registry)
m_config_mismatch = Gauge('wifibox_config_runtime_mismatch', '1 if AP flag mismatch', registry=registry)
m_wifi_connected = Gauge('wifibox_wifi_connected', '1 if wlan0 is connected', registry=registry)
m_wifi_signal = Gauge('wifibox_wifi_signal_percent', 'Wifi signal percentage 0-100', registry=registry)
m_wifi_ssid = Info('wifibox_wifi_ssid', 'Connected WiFi SSID', registry=registry)
m_check_success = Gauge('wifibox_check_success', '1 if all checks passed, 0 if agent failed a check', registry=registry)
m_last_check = Gauge('wifibox_last_check_timestamp_seconds', 'Epoch timestamp of last successful check', registry=registry)
m_agent_info = Info('wifibox_agent_info', 'List of running background apps', registry=registry)

# Hardware Health Metrics
m_cpu_usage = Gauge('wifibox_cpu_usage_percent', 'CPU usage percentage', registry=registry)
m_cpu_temp = Gauge('wifibox_cpu_temp_celsius', 'CPU temperature in Celsius', registry=registry)
m_mem_total = Gauge('wifibox_memory_total_bytes', 'Total memory in bytes', registry=registry)
m_mem_used = Gauge('wifibox_memory_used_bytes', 'Used memory in bytes', registry=registry)
m_mem_avail = Gauge('wifibox_memory_available_bytes', 'Available memory in bytes', registry=registry)
m_disk_total = Gauge('wifibox_disk_total_bytes', 'Total disk space in bytes', registry=registry)
m_disk_used = Gauge('wifibox_disk_used_bytes', 'Used disk space in bytes', registry=registry)
m_disk_free = Gauge('wifibox_disk_free_bytes', 'Free disk space in bytes', registry=registry)

# Globals for tracking state
last_cpu_idle = 0
last_cpu_total = 0
last_known_wg_ip = None

def run_cmd(cmd):
    try:
        result = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return result.stdout.strip(), result.returncode
    except Exception as e:
        return str(e), 1

def get_pi_uid():
    uid = "unknown"
    try:
        with open('/sys/firmware/devicetree/base/serial-number', 'r') as f:
            uid = f.read().replace('\x00', '').strip()
    except Exception:
        try:
            with open('/etc/machine-id', 'r') as f:
                uid = f.read().strip()[:8]
        except Exception:
            pass
    return f"wb-{uid}"

def get_wg_ip():
    out, code = run_cmd("ip -4 addr show wg5")
    if code == 0:
        match = re.search(r'inet\s+([0-9]+(?:\.[0-9]+){3})', out)
        if match:
            return match.group(1)
    return "127.0.0.1"

def upload_identification_http(uid, wg_ip):
    if not HEAD_OFFICE_API_BASE_URL:
        return
    url = f"{HEAD_OFFICE_API_BASE_URL}/upload"
    
    # Matches the server's expected query parameters: ?uid=...&ip=...
    query_params = {
        "uid": uid,
        "ip": wg_ip
    }
    
    try:
        logger.info(f"Sending identity registration to {url} with params {query_params}")
        # The server handles file creation on its end via query params
        response = requests.post(url, params=query_params, timeout=10)
        
        if response.ok:
            logger.info(f"Successfully registered identity for {uid}. Server responded: {response.status_code}")
        else:
            logger.error(f"Registration failed for {uid}. HTTP {response.status_code}: {response.text}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error while registering identity for {uid}: {e}")
    except Exception as e:
        logger.error(f"Unexpected error while registering identity for {uid}: {e}")

def sync_identification():
    global last_known_wg_ip
    current_wg_ip = get_wg_ip()
    uid = get_pi_uid()
    
    uid_dir = os.path.join(ID_FILE_DIR, uid)
    local_file_path = os.path.join(uid_dir, "wifibox_identification.json")
    
    if current_wg_ip != last_known_wg_ip or not os.path.exists(local_file_path):
        logger.info(f"Identity change or missing file detected. Syncing UID {uid} with IP {current_wg_ip}")
        os.makedirs(uid_dir, exist_ok=True)
        
        # Keep a local backup copy on the Pi just in case
        data = [
            {
                "uid": uid,
                "wg_ip": current_wg_ip
            }
        ]
        try:
            with open(local_file_path, 'w') as f:
                json.dump(data, f, indent=2)
                
            # Trigger the HTTP request to the Head Office server
            upload_identification_http(uid, current_wg_ip)
            last_known_wg_ip = current_wg_ip
        except Exception as e:
            logger.error(f"Failed to save local identity JSON: {e}")

def check_internet():
    out, code1 = run_cmd("ping -c 1 -W 2 -I wlan0 8.8.8.8")
    out, code2 = run_cmd("ping -c 1 -W 2 -I eth1 8.8.8.8")
    return 1 if (code1 == 0 or code2 == 0) else 0

def get_vpn_stats():
    out, code = run_cmd("wg show wg5 latest-handshakes")
    if code != 0 or not out: return 0, 0
    try:
        parts = out.split()
        if len(parts) >= 2:
            last_handshake = int(parts[1])
            if last_handshake == 0: return 0, 0
            age = int(time.time()) - last_handshake
            return 1 if age < 180 else 0, age
    except: pass
    return 0, 0

def get_tailscale_stats():
    out, code = run_cmd("systemctl is-active tailscaled")
    if out != "active": return 0
    out, code = run_cmd("tailscale ip -4")
    if code == 0 and out.strip(): return 1
    return 0

def check_ap_interface():
    out, code = run_cmd("cat /sys/class/net/eth0/carrier")
    return 1 if out == "1" else 0

def check_ip_forward():
    out, code = run_cmd("cat /proc/sys/net/ipv4/ip_forward")
    return 1 if out == "1" else 0

def check_nat():
    out, code = run_cmd("iptables -t nat -S | grep MASQUERADE")
    return 1 if code == 0 and "MASQUERADE" in out else 0

def get_dhcp_stats():
    dhcp_up, backend = 0, "none"
    out, code = run_cmd("systemctl is-active dnsmasq")
    if out == "active":
        dhcp_up, backend = 1, "dnsmasq"
    else:
        out, code = run_cmd("systemctl is-active isc-dhcp-server")
        if out == "active": dhcp_up, backend = 1, "isc-dhcp-server"
    m_dhcp_backend.info({'backend': backend})
    return dhcp_up

def get_wifi_stats():
    out, code = run_cmd("iw dev wlan0 link")
    if "Not connected" in out or code != 0:
        m_wifi_ssid.info({'ssid': 'none'})
        return 0, 0
    ssid_match = re.search(r'SSID:\s+(.*)', out)
    if ssid_match: m_wifi_ssid.info({'ssid': ssid_match.group(1)})
    sig_match = re.search(r'signal:\s+(-\d+)\s+dBm', out)
    signal_pct = 0
    if sig_match:
        dbm = int(sig_match.group(1))
        signal_pct = max(0, min(100, 2 * (dbm + 100)))
    return 1, signal_pct

def get_running_apps():
    out, code = run_cmd("ps -eo comm | sort | uniq | grep -E 'dnsmasq|dhcpd|wg|python|sshd|tailscaled'")
    apps = out.replace('\n', ',') if code == 0 else "unknown"
    m_agent_info.info({'apps': apps})

def get_hardware_health():
    global last_cpu_idle, last_cpu_total
    
    try:
        with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
            m_cpu_temp.set(float(f.read().strip()) / 1000.0)
    except: pass

    try:
        with open('/proc/stat', 'r') as f:
            line = f.readline()
            if line.startswith('cpu '):
                parts = [float(i) for i in line.split()[1:]]
                idle = parts[3] + parts[4]
                total = sum(parts)
                
                if last_cpu_total > 0:
                    diff_idle = idle - last_cpu_idle
                    diff_total = total - last_cpu_total
                    if diff_total > 0:
                        usage = 100.0 * (1.0 - (diff_idle / diff_total))
                        m_cpu_usage.set(usage)
                
                last_cpu_idle = idle
                last_cpu_total = total
    except: pass

    try:
        meminfo = {}
        with open('/proc/meminfo', 'r') as f:
            for line in f:
                parts = line.split(':')
                if len(parts) == 2:
                    val = parts[1].strip().split()[0]
                    meminfo[parts[0].strip()] = int(val) * 1024
        
        if 'MemTotal' in meminfo: m_mem_total.set(meminfo['MemTotal'])
        if 'MemAvailable' in meminfo: m_mem_avail.set(meminfo['MemAvailable'])
        if 'MemFree' in meminfo:
            used = meminfo.get('MemTotal', 0) - meminfo.get('MemFree', 0) - meminfo.get('Buffers', 0) - meminfo.get('Cached', 0)
            m_mem_used.set(max(0, used))
    except: pass

    try:
        disk = shutil.disk_usage("/")
        m_disk_total.set(disk.total)
        m_disk_used.set(disk.used)
        m_disk_free.set(disk.free)
    except: pass

def push_pending_data():
    if not PUSHGATEWAY_URL: return
    try:
        with open(TEXT_FILE_PATH, 'rb') as f:
            data = f.read()
            requests.post(f"{PUSHGATEWAY_URL}/metrics/job/wifibox_agent", data=data, timeout=5)
    except: pass

def main():
    start_http_server(PORT, registry=registry)
    os.makedirs(os.path.dirname(TEXT_FILE_PATH), exist_ok=True)
    was_offline = False

    logger.info("Wifibox Agent started.")

    while True:
        try:
            is_online = check_internet()
            m_internet_up.set(is_online)
            
            if is_online:
                sync_identification()
            
            vpn_up, vpn_age = get_vpn_stats()
            m_vpn_up.set(vpn_up)
            m_vpn_handshake_age.set(vpn_age)
            
            m_tailscale_up.set(get_tailscale_stats())
            m_ap_interface_up.set(check_ap_interface())
            m_ip_forward.set(check_ip_forward())
            m_nat_masq.set(check_nat())
            
            arp_out, _ = run_cmd("arp -i eth0 | grep -v incomplete | wc -l")
            try: m_connected_devices.set(int(arp_out) - 1)
            except: m_connected_devices.set(0)
            
            m_dhcp_up.set(get_dhcp_stats())
            m_dhcp_file_present.set(1 if os.path.exists(LEASE_FILE_DHCLIENT) else 0)
            
            sys_wg_out, _ = run_cmd("systemctl is-active wg-quick@wg5")
            m_wg_systemd_up.set(1 if sys_wg_out == "active" else 0)
            
            wifi_up, wifi_sig = get_wifi_stats()
            m_wifi_connected.set(wifi_up)
            m_wifi_signal.set(wifi_sig)
            
            m_config_mismatch.set(1 if not os.path.exists(IS_AP_FLAG_FILE) else 0)
            get_running_apps()
            get_hardware_health()
            
            m_check_success.set(1)
            m_last_check.set(time.time())
            
            write_to_textfile(TEXT_FILE_PATH, registry)
            if is_online and was_offline: push_pending_data()
            was_offline = not is_online
        except Exception as e:
            m_check_success.set(0)
            logger.error(f"Agent loop encountered an error: {e}")
        time.sleep(UPDATE_INTERVAL)

if __name__ == "__main__":
    main()
