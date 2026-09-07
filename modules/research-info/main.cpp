#include <cstdio>
#include <cstring>
#include <string>
#include <utility>
#include <vector>
#ifdef __ANDROID__
#include <sys/system_properties.h>
#endif

static std::string property(const char* key) {
#ifdef __ANDROID__
    char value[PROP_VALUE_MAX] = {};
    __system_property_get(key, value);
    return value;
#else
    (void)key;
    return "";
#endif
}

static std::string json_string(const std::string& value) {
    std::string out = "\"";
    for (unsigned char c : value) {
        if (c == '"' || c == '\\') {
            out += '\\';
            out += static_cast<char>(c);
        } else if (c < 0x20) {
            char escaped[7];
            std::snprintf(escaped, sizeof(escaped), "\\u%04x", c);
            out += escaped;
        } else {
            out += static_cast<char>(c);
        }
    }
    return out + '"';
}

int main(int argc, char** argv) {
    const bool json = argc == 2 && std::strcmp(argv[1], "--json") == 0;
    if (argc == 2 && std::strcmp(argv[1], "--help") == 0) {
        std::puts("Usage: research-info [--json]\nRead-only Android build information.");
        return 0;
    }
    if (argc > 1 && !json) {
        std::fputs("Usage: research-info [--json]\n", stderr);
        return 2;
    }
    const std::vector<std::pair<std::string, std::string>> fields = {
        {"tool_version", "0.1.0"},
        {"android_release", property("ro.build.version.release")},
        {"sdk", property("ro.build.version.sdk")},
        {"build_id", property("ro.build.id")},
        {"fingerprint", property("ro.build.fingerprint")},
        {"abi", property("ro.product.cpu.abi")},
        {"build_flavor", property("ro.build.flavor")},
    };
    if (json) std::puts("{");
    for (size_t i = 0; i < fields.size(); ++i) {
        if (json) {
            std::printf("  %s: %s%s\n", json_string(fields[i].first).c_str(),
                        json_string(fields[i].second).c_str(),
                        i + 1 == fields.size() ? "" : ",");
        } else {
            std::printf("%s=%s\n", fields[i].first.c_str(), fields[i].second.c_str());
        }
    }
    if (json) std::puts("}");
    return 0;
}
