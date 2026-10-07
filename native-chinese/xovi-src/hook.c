#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#ifdef RMTOOL_RUNTIME_CATALOG
#include <stdlib.h>
#include <string.h>
#endif

typedef void *vptr;
typedef const void *cvptr;
typedef const char *ccharptr;
typedef unsigned long ulong;
typedef long slong;
typedef long long i64;

#include "xovi.h"

#define QLOCALE_CHINESE 58

#ifdef RMTOOL_RUNTIME_CATALOG
static const uint16_t data_catalog[] = u"/data/rmtool/xovi-standalone/native-chinese/reMarkable_zh_CN.qm";
static const uint16_t home_catalog[] = u"/home/root/.local/share/rmtool/xovi-standalone/native-chinese/reMarkable_zh_CN.qm";
#else
/* Preserve the published AArch64 artifact's padded path. */
static const uint16_t chinese_catalog[] = {
    '/', 'd', 'a', 't', 'a', '/', 'r', 'm', 't', 'o', 'o', 'l', '/', 'x', 'o', 'v',
    'i', '-', 's', 't', 'a', 'n', 'd', 'a', 'l', 'o', 'n', 'e', '/', 'n', 'a', 't', 'i',
    'v', 'e', '-', 'c', 'h', 'i', 'n', 'e', 's', 'e', '/', '.', '/', '.', '/', '.', '/',
    '.', '/', '.', '/', '.', '/', '.', '/', '.', '/', '.', '/', 'r', 'e', 'M', 'a', 'r',
    'k', 'a', 'b', 'l', 'e', '_', 'z', 'h', '_', 'C', 'N', '.', 'q', 'm', 0
};
#endif
static const uint16_t empty_text[] = {0};

/* Oversized aligned storage; Qt constructs the process-lifetime objects. */
static _Alignas(16) unsigned char chinese_catalog_qstring[64];
static _Alignas(16) unsigned char empty_qstring[64];
static bool qstrings_ready;

static bool load_chinese_catalog(void *translator) {
    if (!qstrings_ready) {
        return false;
    }
    return (bool) $_ZN11QTranslator4loadERK7QStringS2_S2_S2_(
        translator,
        chinese_catalog_qstring,
        empty_qstring,
        empty_qstring,
        empty_qstring
    );
}

void _xovi_construct(void) {
#ifdef RMTOOL_RUNTIME_CATALOG
    const char *root = getenv("XOVI_ROOT");
    bool home = root && strcmp(root, "/home/root/.local/share/rmtool/xovi-standalone") == 0;
    const uint16_t *chinese_catalog = home ? home_catalog : data_catalog;
    i64 catalog_length = home ? sizeof(home_catalog) / sizeof(home_catalog[0]) - 1
                              : sizeof(data_catalog) / sizeof(data_catalog[0]) - 1;
#else
    i64 catalog_length = sizeof(chinese_catalog) / sizeof(chinese_catalog[0]) - 1;
#endif
    $_ZN7QStringC1EPK5QCharx(
        chinese_catalog_qstring,
        chinese_catalog,
        catalog_length
    );
    $_ZN7QStringC1EPK5QCharx(empty_qstring, empty_text, 0);
    qstrings_ready = true;
    /* Intentionally no destructor: both QStrings live until process exit. */
}

bool override$_ZN11QTranslator4loadERK7QLocaleRK7QStringS5_S5_S5_(
    void *translator,
    const void *locale,
    const void *filename,
    const void *prefix,
    const void *directory,
    const void *suffix
) {
    if ((int) $_ZNK7QLocale8languageEv(locale) == QLOCALE_CHINESE) {
        if (load_chinese_catalog(translator)) {
            return true;
        }
    }
    return (bool) $_ZN11QTranslator4loadERK7QLocaleRK7QStringS5_S5_S5_(
        translator, locale, filename, prefix, directory, suffix
    );
}
