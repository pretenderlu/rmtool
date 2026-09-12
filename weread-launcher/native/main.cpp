#include "WeReadLauncher.h"

#include <QCryptographicHash>
#include <QFile>
#include <QFileInfo>
#include <QProcess>
#include <QProcessEnvironment>
#include <QQmlEngine>
#include <cerrno>
#include <cstdio>
#include <cstring>
#include <dlfcn.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

int qInitResources_weread_assets();

namespace {
constexpr auto kBinary = "/home/root/.local/opt/remarkable-weread/bin/remarkable-weread";
constexpr auto kLauncher = "/home/root/.local/opt/remarkable-weread/bin/start-remarkable-weread.sh";
constexpr auto kUnit = "/home/root/.local/opt/remarkable-weread/systemd/remarkable-weread-app.service";
constexpr auto kFastShim = "/data/rmtool/xovi-standalone/helpers/rmtool-weread-fast.so";
constexpr auto kFastDropinDirectory = "/run/systemd/system/remarkable-weread-app.service.d";
constexpr auto kFastDropin = "/run/systemd/system/remarkable-weread-app.service.d/90-rmtool-fast.conf";
constexpr auto kStartupPending = "/data/rmtool/xovi-standalone/startup.pending";
constexpr int kModeNormal = 0;
constexpr int kModeMono = 1;
constexpr int kModeAnimation = 2;

#ifndef RMTOOL_WEREAD_FAST_SHA256
#error RMTOOL_WEREAD_FAST_SHA256 must be supplied by the native build
#endif

struct TrustedFile {
    const char *path;
    const char *sha256;
    bool executable;
};

constexpr TrustedFile kTrustedFiles[] = {
    {kBinary, "5a117d08c8503bad49b65a54357cdc1698b0ae3e52550d704fe9c6262f061ac7", true},
    {kLauncher, "8fa38f84242cce01f9e6ef547e075a166d8d61255e3df8f9e1292d9876c337e8", true},
    {kUnit, "5ae39c435f1f47e34f2805b6eb8c0cbdeca398ff22d99c7a572321380547b185", false},
};

QString verify(const TrustedFile &trusted)
{
    QFileInfo info(QString::fromUtf8(trusted.path));
    if (!info.exists() || !info.isFile() || info.isSymLink() || info.ownerId() != 0) {
        return QStringLiteral("未检测到兼容的微信读书官方安装");
    }
    if (trusted.executable && !info.isExecutable()) {
        return QStringLiteral("微信读书官方启动文件不可执行");
    }
    QFile file(info.filePath());
    if (!file.open(QIODevice::ReadOnly)) {
        return QStringLiteral("无法读取微信读书官方安装");
    }
    QCryptographicHash hash(QCryptographicHash::Sha256);
    if (!hash.addData(&file)
        || hash.result().toHex() != QByteArray::fromRawData(trusted.sha256, 64)) {
        return QStringLiteral("微信读书官方安装版本不受支持");
    }
    return {};
}

bool supportedRefreshPages(int value)
{
    return value == 0 || value == 5 || value == 10 || value == 20 || value == 30;
}

bool configureFastMode(int mode, int refreshPages, QString *error)
{
    if (mode == kModeNormal) {
        if (::unlink(kFastDropin) != 0 && errno != ENOENT) {
            *error = QStringLiteral("无法清除微信读书极速模式配置");
            return false;
        }
        return true;
    }

    if (::mkdir(kFastDropinDirectory, 0755) != 0 && errno != EEXIST) {
        *error = QStringLiteral("无法准备微信读书极速模式配置");
        return false;
    }
    char temporary[256];
    if (std::snprintf(temporary, sizeof(temporary), "%s.%ld", kFastDropin,
                      static_cast<long>(::getpid())) >= static_cast<int>(sizeof(temporary))) {
        *error = QStringLiteral("无法保存微信读书极速模式配置");
        return false;
    }
    const int descriptor = ::open(temporary, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
    if (descriptor < 0) {
        *error = QStringLiteral("无法保存微信读书极速模式配置");
        return false;
    }
    char content[384];
    const int contentSize = std::snprintf(
        content, sizeof(content),
        "[Service]\n"
        "Environment=LD_PRELOAD=/data/rmtool/xovi-standalone/helpers/rmtool-weread-fast.so\n"
        "Environment=RMTOOL_WEREAD_MODE=%d\n"
        "Environment=RMTOOL_WEREAD_REFRESH_PAGES=%d\n",
        mode, refreshPages);
    if (contentSize <= 0 || contentSize >= static_cast<int>(sizeof(content))) {
        ::close(descriptor);
        ::unlink(temporary);
        *error = QStringLiteral("无法保存微信读书极速模式配置");
        return false;
    }
    const bool saved = ::write(descriptor, content, static_cast<size_t>(contentSize))
            == static_cast<ssize_t>(contentSize)
        && ::fsync(descriptor) == 0
        && ::fchmod(descriptor, 0644) == 0;
    ::close(descriptor);
    if (!saved || ::rename(temporary, kFastDropin) != 0) {
        ::unlink(temporary);
        *error = QStringLiteral("无法保存微信读书极速模式配置");
        return false;
    }
    return true;
}

bool clearStartupGuard(QString *error)
{
    struct stat metadata {};
    if (::lstat(kStartupPending, &metadata) != 0) {
        if (errno == ENOENT) {
            return true;
        }
        *error = QStringLiteral("无法确认 rmtool 插件启动状态");
        return false;
    }
    if (!S_ISREG(metadata.st_mode)
        || metadata.st_uid != 0
        || metadata.st_gid != 0
        || metadata.st_size != 0
        || (metadata.st_mode & 0777) != 0600) {
        *error = QStringLiteral("rmtool 插件启动保护状态异常");
        return false;
    }
    if (::unlink(kStartupPending) != 0) {
        *error = QStringLiteral("无法结束 rmtool 插件启动观察期");
        return false;
    }
    return true;
}
}

QString WeReadLauncher::validateInstallation() const
{
    for (const auto &trusted : kTrustedFiles) {
        const QString error = verify(trusted);
        if (!error.isEmpty()) {
            return error;
        }
    }
    return {};
}

bool WeReadLauncher::available() const
{
    return validateInstallation().isEmpty();
}

QString WeReadLauncher::unavailableReason() const
{
    return validateInstallation();
}

QString WeReadLauncher::validateFastMode() const
{
    return verify({kFastShim, RMTOOL_WEREAD_FAST_SHA256, false});
}

bool WeReadLauncher::launch(int mode, int refreshPages)
{
    if ((mode != kModeNormal && mode != kModeMono && mode != kModeAnimation)
        || !supportedRefreshPages(refreshPages)
        || (mode == kModeNormal && refreshPages != 0)) {
        m_lastError = QStringLiteral("微信读书启动参数无效");
        return false;
    }
    m_lastError = validateInstallation();
    if (!m_lastError.isEmpty()) {
        return false;
    }
    if (mode != kModeNormal && !validateFastMode().isEmpty()) {
        m_lastError = QStringLiteral("微信读书快刷模块不可用，请使用普通模式");
        return false;
    }
    // The bridge is callable only after Xovi and this Settings page loaded.
    if (!clearStartupGuard(&m_lastError)) {
        return false;
    }
    if (!configureFastMode(mode, refreshPages, &m_lastError)) {
        return false;
    }

    QProcess process;
    QProcessEnvironment environment = QProcessEnvironment::systemEnvironment();
    environment.remove(QStringLiteral("LD_PRELOAD"));
    environment.remove(QStringLiteral("XOVI_ROOT"));
    environment.remove(QStringLiteral("QT_RESOURCE_REBUILDER_PATH"));
    process.setProcessEnvironment(environment);
    process.setProgram(QString::fromUtf8(kLauncher));
    process.setArguments({});
    if (!process.startDetached()) {
        m_lastError = QStringLiteral("无法启动微信读书官方服务");
        return false;
    }
    return true;
}

extern "C" void _xovi_construct()
{
    qInitResources_weread_assets();
    qmlRegisterType<WeReadLauncher>("cn.rmtool.WeReadLauncher", 1, 0, "WeReadLauncher");
}

extern "C" char _xovi_shouldLoad()
{
    return dlsym(RTLD_DEFAULT, "_Z21qRegisterResourceDataiPKhS0_S0_") != nullptr;
}

extern "C" __attribute__((section(".xovi_info"))) const int EXTENSIONVERSION = 0x00000100;
__attribute__((section(".xovi"))) const char *LINKTABLENAMES = "Ephony\0\0";
__attribute__((section(".xovi"))) const void *LINKTABLEVALUES[] = {(void *)1, (void *)0};

#include "moc_WeReadLauncher.cpp"
