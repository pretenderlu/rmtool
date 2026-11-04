#include "highlight_identity.h"

int main(int argc, char **argv) {
    if (argc != 4) return 2;
    return highlight_identity_matches(argv[1], argv[2], argv[3]) ? 0 : 1;
}
