#include <cassert>
#include "TrustedFileCache.h"

int main()
{
    TrustedFileCache cache;
    TrustedFileIdentity identity {1, 2, 43 * 1024 * 1024, 3, 4, 5, 6, 0755, 0, 0};
    assert(cache.needsHash(identity, false));
    cache.identity = identity;
    cache.verified = true;
    assert(!cache.needsHash(identity, false));
    assert(cache.needsHash(identity, true));
    std::uint64_t TrustedFileIdentity::*fields[] = {
        &TrustedFileIdentity::device, &TrustedFileIdentity::inode, &TrustedFileIdentity::size,
        &TrustedFileIdentity::modifiedSeconds, &TrustedFileIdentity::modifiedNanos,
        &TrustedFileIdentity::changedSeconds, &TrustedFileIdentity::changedNanos,
        &TrustedFileIdentity::mode, &TrustedFileIdentity::uid, &TrustedFileIdentity::gid,
    };
    for (auto field : fields) {
        auto changed = identity;
        ++(changed.*field);
        assert(cache.needsHash(changed, false));
    }
    cache.verified = false;
    assert(cache.needsHash(identity, false));
}
