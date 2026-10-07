#include <assert.h>
#include "hook.c"

static const char *expected;

static void capture_qstring(void *destination, const void *text, i64 length) {
    if (destination == chinese_catalog_qstring) {
        const uint16_t *path = text;
        assert(length == (i64) strlen(expected));
        for (i64 i = 0; i < length; ++i) {
            assert(path[i] == (unsigned char) expected[i]);
        }
        assert(path[length] == 0);
    } else {
        assert(destination == empty_qstring && length == 0);
    }
}

const void *LINKTABLEVALUES[10] = {[7] = (const void *) capture_qstring};

int main(int argc, char **argv) {
    assert(argc == 2);
    expected = argv[1];
    _xovi_construct();
    assert(qstrings_ready);
    return 0;
}
