/*
 * Regenerates the FRAMES in test_frdid.py: French Direct Remote ID beacon
 * frames from opendroneid-core-c's own encoder, printed as name<TAB>hex.
 *
 * Build against a checkout of https://github.com/opendroneid/opendroneid-core-c
 * (the fixtures used commit 6484f26):
 *
 *   L=opendroneid-core-c/libopendroneid
 *   cc -I$L -o gen_frdid gen_frdid.c $L/wifi.c $L/opendroneid.c -lm
 *   ./gen_frdid
 *
 * The inputs below are the EXPECTED values in test_frdid.py.
 */
#include <stdio.h>
#include "opendroneid.h"
static void emit(const char *name, const FRDID_UAS_Data *d) {
    uint8_t buf[512];
    int n = frdid_wifi_build_beacon_frame(d, "\x02\x11\x22\x33\x44\x55", "FR-DRONE", 8, 100, buf, sizeof buf);
    if (n < 0) { fprintf(stderr, "%s: error %d\n", name, n); return; }
    printf("%s\t", name);
    for (int i = 0; i < n; i++) printf("%02x", buf[i]);
    printf("\n");
}
int main(void) {
    FRDID_UAS_Data full = { "ABC000000000000000000000000001", "1581F5FJD21234567890",
        48.8583701, 2.2944813, 152, 45, 48.85800, 2.29400, 12, 271 };
    FRDID_UAS_Data south_west = { "XYZ123ABCDEFGHIJKLMNOPQRSTUVWX", NULL,
        -33.8567844, -70.2153, -12, 3, -33.857, -70.216, 0, 359 };
    FRDID_UAS_Data id_only = { "ABC000000000000000000000000002", NULL,
        0, 0, INV_ALT, INV_ALT, 0, 0, INV_SPEED_H, INV_DIR };
    FRDID_UAS_Data serial_only_fast = { NULL, "SERIAL-ONLY-77",
        45.5, 5.25, 900, 120, 45.49, 5.24, 100, 0 };
    emit("full", &full); emit("south_west", &south_west);
    emit("id_only", &id_only); emit("serial_only_fast", &serial_only_fast);
    return 0;
}
