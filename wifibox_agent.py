import time
import subprocess
import os
import re
import requests
from prometheus_client import start_http_server, Gauge, Info, CollectorRegistry, write_to_textfile

PORT = 9101
UPDATE_INTERVAL = 300
TEXT_FILE_PATH = "/home/oldendome/wifibox-agent/meteric-data.txt"
IS_AP_FLAG_FILE = "/home/oldendome/wifibox_is_ap.txt"
LEASE_FILE_DHCLIENT = "/var/lib/dhcp/dhclient.leases"
PUSHGATEWAY_URL = "" 

registry = CollectorRegistry()

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

def run_cmd(cmd):
    try:
        result = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return result.stdout.strip(), result.returncode
    except Exception as e:
        return str(e), 1

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

    while True:
        try:
            is_online = check_internet()
            m_internet_up.set(is_online)
            
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
            
            m_check_success.set(1)
            m_last_check.set(time.time())
            
            write_to_textfile(TEXT_FILE_PATH, registry)
            if is_online and was_offline: push_pending_data()
            was_offline = not is_online
        except Exception:
            m_check_success.set(0)
        time.sleep(UPDATE_INTERVAL)

if __name__ == "__main__":
    main()