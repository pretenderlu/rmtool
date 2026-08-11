#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef void *vptr;
typedef const void *cvptr;
typedef const char *ccharptr;
typedef unsigned long ulong;
typedef long slong;
typedef long long i64;

#include "xovi.h"

#define QLOCALE_CHINESE 58

/* Dot segments keep the audited UTF-16 artifact length unchanged after moving to /data. */
static const uint16_t chinese_catalog[] = {
    '/', 'd', 'a', 't', 'a', '/', 'r', 'm', 't', 'o', 'o', 'l', '/', 'x', 'o', 'v',
    'i', '-', 's', 't', 'a', 'n', 'd', 'a', 'l', 'o', 'n', 'e', '/', 'n', 'a', 't', 'i',
    'v', 'e', '-', 'c', 'h', 'i', 'n', 'e', 's', 'e', '/', '.', '/', '.', '/', '.', '/',
    '.', '/', '.', '/', '.', '/', '.', '/', '.', '/', '.', '/', 'r', 'e', 'M', 'a', 'r',
    'k', 'a', 'b', 'l', 'e', '_', 'z', 'h', '_', 'C', 'N', '.', 'q', 'm', 0
};
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
    $_ZN7QStringC1EPK5QCharx(
        chinese_catalog_qstring,
        chinese_catalog,
        (i64) (sizeof(chinese_catalog) / sizeof(chinese_catalog[0]) - 1)
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
