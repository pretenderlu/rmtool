#pragma once
#include <cstdint>

struct TrustedFileIdentity {
    std::uint64_t device, inode, size, modifiedSeconds, modifiedNanos;
    std::uint64_t changedSeconds, changedNanos, mode, uid, gid;

    bool operator==(const TrustedFileIdentity &other) const {
        return device == other.device && inode == other.inode && size == other.size
            && modifiedSeconds == other.modifiedSeconds && modifiedNanos == other.modifiedNanos
            && changedSeconds == other.changedSeconds && changedNanos == other.changedNanos
            && mode == other.mode && uid == other.uid && gid == other.gid;
    }
};

struct TrustedFileCache {
    TrustedFileIdentity identity {};
    bool verified = false;

    bool needsHash(const TrustedFileIdentity &current, bool force) const {
        return force || !verified || !(identity == current);
    }
};
