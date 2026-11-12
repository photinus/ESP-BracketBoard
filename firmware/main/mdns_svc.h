// mDNS: <host>.local, _bracketboard._tcp (TXT v=1, path=/ws) and _http._tcp, port 80.
#pragma once
#include <string>
#include "esp_err.h"

namespace bbfw {

// Call after Wi-Fi/netif initialisation (esp_netif + default event loop exist).
esp_err_t mdns_svc_start(const std::string& hostname);

}  // namespace bbfw
