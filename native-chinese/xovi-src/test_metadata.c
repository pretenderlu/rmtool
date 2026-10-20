#include <assert.h>
#include <string.h>
#include "xovi.c"

void override$_ZN11QTranslator4loadERK7QLocaleRK7QStringS5_S5_S5_(void) {}

int main(void) {
    int found = 0;
    int index = 1;
    for (const char *name = LINKTABLENAMES; *name; name += strlen(name) + 1, ++index) {
        if (*name != 'O') continue;
        const struct XoviMetadataEntry **chain = METADATAVALUES[index];
        assert(chain && chain[0] && chain[1] == NULL);
        assert(strcmp(chain[0]->name, "$argsize") == 0);
        assert(chain[0]->type == METADATA_TYPE_INT && chain[0]->value.i == 6);
        ++found;
    }
    assert(found == 1);
    return 0;
}
