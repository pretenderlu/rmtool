#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <dlfcn.h>
#include <time.h>

namespace {
using Constructor = void (*)(void *, void *);
using SetMode = void (*)(void *, int);
using SetModeForRegion = void (*)(void *, const void *, int);
using Activate = void (*)(void *, const void *, int, void **);
using ClassName = const char *(*)(const void *);

struct Rect {
    int left;
    int top;
    int right;
    int bottom;
};

using BoundingRect = Rect (*)(const void *);

constexpr auto kConstructor = "_ZN16EPScreenModeItemC1EP10QQuickItem";
constexpr auto kSetMode = "_ZN16EPScreenModeItem7setModeENS_4ModeE";
constexpr auto kSetModeForRegion =
    "_ZN15EPScreenModeMap16setModeForRegionERK7QRegion12EPScreenMode";
constexpr auto kActivate = "_ZN11QMetaObject8activateEP7QObjectPKS_iPPv";
constexpr auto kClassName = "_ZNK11QMetaObject9classNameEv";
constexpr auto kBoundingRect = "_ZNK7QRegion12boundingRectEv";
constexpr int kModePen = 0;
constexpr int kModeMono = 1;
constexpr int kModeAnimation = 2;
constexpr int kModeContent = 4;
constexpr int kRouteChangedSignal = 0;
constexpr int kPaginationSignal = 12;
constexpr int kPenDownSignal = 4;
constexpr int kPenMoveSignal = 5;
constexpr int kPenUpSignal = 6;
constexpr unsigned long long kPenTailMs = 50;
constexpr unsigned long long kDuplicateWindowMs = 400;

struct RefreshConfiguration {
    int mode = kModeMono;
    unsigned int interval = 0;
};

struct PageState {
    bool paginationPending = false;
    bool initialized = false;
    bool hasFullScreen = false;
    Rect fullScreen{};
    unsigned int pageCount = 0;
    unsigned long long lastCandidateMs = 0;
};

std::atomic_flag pageLock = ATOMIC_FLAG_INIT;
PageState pageState;
std::atomic<unsigned long long> penDeadlineMs{0};

struct PageLockGuard {
    PageLockGuard()
    {
        while (pageLock.test_and_set(std::memory_order_acquire)) {
        }
    }

    ~PageLockGuard()
    {
        pageLock.clear(std::memory_order_release);
    }
};

unsigned long long monotonicMilliseconds()
{
    timespec now{};
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) {
        return 0;
    }
    return static_cast<unsigned long long>(now.tv_sec) * 1000ULL
        + static_cast<unsigned long long>(now.tv_nsec) / 1000000ULL;
}

bool isPenSignal(int localSignalIndex)
{
    return localSignalIndex == kPenDownSignal
        || localSignalIndex == kPenMoveSignal
        || localSignalIndex == kPenUpSignal;
}

void observePenSignal(int localSignalIndex)
{
    if (!isPenSignal(localSignalIndex)) {
        return;
    }
    const auto now = monotonicMilliseconds();
    if (now == 0) {
        return;
    }
    const auto deadline = now + kPenTailMs;
    auto current = penDeadlineMs.load(std::memory_order_relaxed);
    while (current < deadline
           && !penDeadlineMs.compare_exchange_weak(
               current, deadline, std::memory_order_release,
               std::memory_order_relaxed)) {
    }
}

bool penWindowActive()
{
    const auto now = monotonicMilliseconds();
    return now != 0
        && now < penDeadlineMs.load(std::memory_order_acquire);
}

RefreshConfiguration configuration()
{
    static const RefreshConfiguration value = [] {
        RefreshConfiguration result;
        const char *mode = std::getenv("RMTOOL_WEREAD_MODE");
        if (mode && std::strcmp(mode, "2") == 0) {
            result.mode = kModeAnimation;
        }
        const char *interval = std::getenv("RMTOOL_WEREAD_REFRESH_PAGES");
        if (interval && std::strcmp(interval, "5") == 0) {
            result.interval = 5;
        } else if (interval && std::strcmp(interval, "10") == 0) {
            result.interval = 10;
        } else if (interval && std::strcmp(interval, "20") == 0) {
            result.interval = 20;
        } else if (interval && std::strcmp(interval, "30") == 0) {
            result.interval = 30;
        }
        return result;
    }();
    return value;
}

template <typename Function>
Function resolve(const char *symbol)
{
    auto function = reinterpret_cast<Function>(dlsym(RTLD_NEXT, symbol));
    if (!function) {
        std::fprintf(stderr, "[rmtool-weread-fast] missing ABI symbol %s\n", symbol);
        std::abort();
    }
    return function;
}

void logConfigurationOnce()
{
    static std::atomic_flag logged = ATOMIC_FLAG_INIT;
    if (!logged.test_and_set()) {
        const auto config = configuration();
        std::fprintf(stderr, "[rmtool-weread-fast] mode=%d refresh_pages=%u\n",
                     config.mode, config.interval);
    }
}

bool sameRect(const Rect &left, const Rect &right)
{
    return left.left == right.left && left.top == right.top
        && left.right == right.right && left.bottom == right.bottom;
}

bool preserveFullRefresh(const Rect &bounds)
{
    if (bounds.left != 0 || bounds.top != 0
        || bounds.right < bounds.left || bounds.bottom < bounds.top) {
        return false;
    }

    const auto now = monotonicMilliseconds();
    PageLockGuard lock;
    if (!pageState.paginationPending) {
        return false;
    }

    if (!pageState.hasFullScreen) {
        pageState.fullScreen = bounds;
        pageState.hasFullScreen = true;
    } else if (!sameRect(pageState.fullScreen, bounds)) {
        return false;
    }
    pageState.paginationPending = false;

    if (!pageState.initialized) {
        pageState.initialized = true;
        pageState.lastCandidateMs = now;
        return false;
    }
    if (pageState.lastCandidateMs != 0 && now >= pageState.lastCandidateMs
        && now - pageState.lastCandidateMs <= kDuplicateWindowMs) {
        return false;
    }
    pageState.lastCandidateMs = now;

    const auto interval = configuration().interval;
    if (interval == 0) {
        return false;
    }
    ++pageState.pageCount;
    if (pageState.pageCount < interval) {
        return false;
    }
    pageState.pageCount = 0;
    return true;
}

void observeAppControllerSignal(int localSignalIndex)
{
    PageLockGuard lock;
    if (localSignalIndex == kRouteChangedSignal) {
        pageState = {};
    } else if (localSignalIndex == kPaginationSignal) {
        pageState.paginationPending = true;
    }
}
}

extern "C" void rmtoolSetMode(void *self, int)
    __asm__("_ZN16EPScreenModeItem7setModeENS_4ModeE");

extern "C" void rmtoolSetMode(void *self, int requestedMode)
{
    logConfigurationOnce();
    resolve<SetMode>(kSetMode)(
        self, requestedMode == kModePen || penWindowActive()
            ? kModePen : configuration().mode);
}

extern "C" void rmtoolConstructScreenModeItem(void *self, void *parent)
    __asm__("_ZN16EPScreenModeItemC1EP10QQuickItem");

extern "C" void rmtoolConstructScreenModeItem(void *self, void *parent)
{
    resolve<Constructor>(kConstructor)(self, parent);
    logConfigurationOnce();
}

extern "C" void rmtoolSetModeForRegion(void *self, const void *region, int)
    __asm__("_ZN15EPScreenModeMap16setModeForRegionERK7QRegion12EPScreenMode");

extern "C" void rmtoolSetModeForRegion(void *self, const void *region,
                                        int requestedMode)
{
    int effectiveMode = requestedMode == kModePen
        ? kModePen : configuration().mode;
    if (requestedMode == kModeContent) {
        const auto boundingRect =
            reinterpret_cast<BoundingRect>(dlsym(RTLD_NEXT, kBoundingRect));
        if (boundingRect && preserveFullRefresh(boundingRect(region))) {
            effectiveMode = requestedMode;
        }
    }
    if (penWindowActive()) {
        effectiveMode = kModePen;
    }
    logConfigurationOnce();
    resolve<SetModeForRegion>(kSetModeForRegion)(self, region, effectiveMode);
}

extern "C" void rmtoolActivate(void *sender, const void *metaObject,
                                int localSignalIndex, void **arguments)
    __asm__("_ZN11QMetaObject8activateEP7QObjectPKS_iPPv");

extern "C" void rmtoolActivate(void *sender, const void *metaObject,
                                int localSignalIndex, void **arguments)
{
    const auto activate = resolve<Activate>(kActivate);
    const auto className = resolve<ClassName>(kClassName);
    if (metaObject) {
        const auto name = className(metaObject);
        if (name && std::strcmp(name, "AppController") == 0) {
            observeAppControllerSignal(localSignalIndex);
        } else if (name && std::strcmp(name, "PenInput") == 0) {
            observePenSignal(localSignalIndex);
        }
    }
    activate(sender, metaObject, localSignalIndex, arguments);
}
