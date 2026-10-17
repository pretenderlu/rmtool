#include "QrUpload.h"

#include <QCoreApplication>
#include <QCryptographicHash>
#include <QDateTime>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QJsonObject>
#include <QProcessEnvironment>
#include <QQmlEngine>
#include <QRegularExpression>
#include <QSaveFile>
#include <QUrl>
#include <arpa/inet.h>
#include <cstring>
#include <dlfcn.h>
#include <ifaddrs.h>
#include <net/if.h>
#include <unistd.h>

#ifndef RMTOOL_QR_RECEIVER_SHA256
#error RMTOOL_QR_RECEIVER_SHA256 must be supplied by the canary build
#endif

namespace {
constexpr auto kReceiver = "/data/rmtool/xovi-standalone/helpers/rmtool-qr-upload";
constexpr auto kPreference = "/home/root/.rmtool-qr-upload-web-state";
constexpr auto kRestoreDisabled = "rmtool-qr-upload:restore-web-disabled:v1\n";
constexpr auto kStateDir = "/run/rmtool-qr-upload-ui";
constexpr auto kState = "/run/rmtool-qr-upload-ui/session.json";

QString wifiAddress()
{
    ifaddrs *interfaces = nullptr;
    if (getifaddrs(&interfaces) != 0) return {};
    QString result;
    for (auto *item = interfaces; item; item = item->ifa_next) {
        if (!item->ifa_addr || item->ifa_addr->sa_family != AF_INET
            || std::strcmp(item->ifa_name, "wlan0") != 0
            || !(item->ifa_flags & IFF_UP) || !(item->ifa_flags & IFF_RUNNING)) continue;
        const auto address = reinterpret_cast<sockaddr_in *>(item->ifa_addr)->sin_addr;
        const auto value = ntohl(address.s_addr);
        if ((value >> 24) != 10 && (value >> 20) != 0xac1 && (value >> 16) != 0xc0a8) continue;
        char text[INET_ADDRSTRLEN] = {};
        if (!inet_ntop(AF_INET, &address, text, sizeof(text))) continue;
        if (!result.isEmpty()) { result.clear(); break; }
        result = QString::fromLatin1(text);
    }
    freeifaddrs(interfaces);
    return result;
}

bool trustedReceiver()
{
    QFileInfo info(QString::fromLatin1(kReceiver));
    if (!info.isFile() || info.isSymLink() || info.ownerId() != 0 || !info.isExecutable()
        || (info.permissions() & (QFile::WriteGroup | QFile::WriteOther))
        || info.size() > 10 * 1024 * 1024) return false;
    QFile file(info.filePath());
    QCryptographicHash hash(QCryptographicHash::Sha256);
    return file.open(QIODevice::ReadOnly) && hash.addData(&file)
        && hash.result().toHex() == QByteArray(RMTOOL_QR_RECEIVER_SHA256);
}
}

QrUpload::QrUpload(QObject *parent) : QObject(parent), m_status(QStringLiteral("未开启"))
{
    m_poll.setInterval(1000);
    connect(&m_poll, &QTimer::timeout, this, &QrUpload::readSession);
}

QrUpload::~QrUpload()
{
    stop();
    if (m_process) m_process->disconnect(this);
}

void QrUpload::setSettings(QObject *settings)
{
    if (m_settings || !settings || !settings->property("webInterfaceEnabled").isValid()) return;
    m_settings = settings;
    if (!restoreImportPreference()) {
        m_status = QStringLiteral("原生导入设置无法恢复，请先通过 rmtool 检查");
        emit changed();
    }
}

bool QrUpload::restoreImportPreference()
{
    QFileInfo info(QString::fromLatin1(kPreference));
    if (!info.exists() && !info.isSymLink()) return true;
    if (!m_settings || !info.isFile() || info.isSymLink() || info.ownerId() != 0
        || info.size() > 64 || (info.permissions() & (QFile::ReadGroup | QFile::WriteGroup
            | QFile::ReadOther | QFile::WriteOther))) return false;
    QFile file(info.filePath());
    if (!file.open(QIODevice::ReadOnly) || file.read(65) != QByteArray(kRestoreDisabled)) return false;
    file.close();
    return m_settings->setProperty("webInterfaceEnabled", false) && QFile::remove(info.filePath());
}

bool QrUpload::enableImportPreference()
{
    if (!m_settings || !restoreImportPreference()) return false;
    if (m_settings->property("webInterfaceEnabled").toBool()) return true;
    QSaveFile file(QString::fromLatin1(kPreference));
    file.setDirectWriteFallback(false);
    if (!file.open(QIODevice::WriteOnly) || !file.setPermissions(QFile::ReadOwner | QFile::WriteOwner)
        || file.write(kRestoreDisabled) != qint64(std::strlen(kRestoreDisabled)) || !file.commit()) return false;
    return m_settings->setProperty("webInterfaceEnabled", true);
}

void QrUpload::start()
{
    if (busy()) return;
    const auto address = wifiAddress();
    if (address.isEmpty()) {
        m_status = QStringLiteral("请先连接 Wi-Fi");
        emit changed();
        return;
    }
    if (!trustedReceiver()) {
        m_status = QStringLiteral("接收程序校验失败，请通过 rmtool 修复");
        emit changed();
        return;
    }
    if (QFileInfo::exists(QString::fromLatin1(kStateDir))) {
        m_status = QStringLiteral("上次接收尚未清理，请稍后重试或通过 rmtool 检查");
        emit changed();
        return;
    }
    m_stopping = false;
    m_importPrepared = false;
    m_filename.clear();
    m_transferDetail.clear();
    m_progress = -1;
    m_origin = QStringLiteral("http://") + address + QStringLiteral(":8765");
    m_status = QStringLiteral("正在开启接收");
    auto *process = new QProcess(QCoreApplication::instance());
    m_process = process;
    auto environment = QProcessEnvironment::systemEnvironment();
    for (const auto *name : {"LD_PRELOAD", "XOVI_ROOT", "QT_RESOURCE_REBUILDER_PATH",
                            "QML_XHR_ALLOW_FILE_READ", "QML_XHR_ALLOW_FILE_WRITE"}) {
        environment.remove(QString::fromLatin1(name));
    }
    process->setProcessEnvironment(environment);
    process->setStandardOutputFile(QProcess::nullDevice());
    process->setProcessChannelMode(QProcess::ForwardedErrorChannel);
    connect(process, &QProcess::started, this, [this, process]() {
        if (m_stopping) process->terminate();
    });
    connect(process, &QProcess::finished, this, [this](int code, QProcess::ExitStatus) {
        const bool restored = restoreImportPreference();
        m_poll.stop();
        m_process = nullptr;
        m_url.clear();
        m_expires.clear();
        m_transferDetail.clear();
        m_progress = -1;
        m_status = m_stopping ? QStringLiteral("接收已停止")
            : code == 0 ? QStringLiteral("接收已结束")
            : QStringLiteral("接收未能启动或已中断，请检查 Wi-Fi 和系统导入服务");
        if (!restored) m_status = QStringLiteral("接收已停止，但原生导入设置恢复失败，请通过 rmtool 检查");
        emit changed();
    });
    connect(process, &QProcess::finished, process, &QObject::deleteLater);
    connect(process, &QProcess::errorOccurred, this, [this, process](QProcess::ProcessError error) {
        if (error != QProcess::FailedToStart) return;
        restoreImportPreference();
        m_poll.stop();
        m_process = nullptr;
        m_status = QStringLiteral("无法启动接收程序");
        process->deleteLater();
        emit changed();
    });
    process->start(QString::fromLatin1(kReceiver),
        {QStringLiteral("--listen"), address + QStringLiteral(":8765"),
         QStringLiteral("--state-dir"), QString::fromLatin1(kStateDir),
         QStringLiteral("--activate-native"),
         QStringLiteral("--parent-pid"), QString::number(::getpid())});
    m_poll.start();
    emit changed();
}

void QrUpload::stop()
{
    restoreImportPreference();
    m_poll.stop();
    m_url.clear();
    m_expires.clear();
    m_transferDetail.clear();
    m_progress = -1;
    if (m_process && !m_stopping) {
        m_stopping = true;
        m_status = QStringLiteral("正在停止接收");
        m_process->terminate();
        auto *process = m_process.data();
        auto *deadline = new QTimer(process);
        deadline->setSingleShot(true);
        deadline->setInterval(4000);
        connect(deadline, &QTimer::timeout, process, [process]() {
            if (process->state() != QProcess::NotRunning) process->kill();
        });
        deadline->start();
    }
    emit changed();
}

void QrUpload::readSession()
{
    if (!m_process || m_stopping) return;
    if (!m_importPrepared) {
        QFileInfo ready(QStringLiteral("/run/rmtool-qr-upload-ui/native-ready"));
        if (!ready.exists()) return;
        QFile marker(ready.filePath());
        if (!ready.isFile() || ready.isSymLink() || ready.ownerId() != 0 || ready.size() != 6
            || (ready.permissions() & (QFile::ReadGroup | QFile::WriteGroup | QFile::ReadOther | QFile::WriteOther))
            || !marker.open(QIODevice::ReadOnly) || marker.read(7) != QByteArray("ready\n")
            || !enableImportPreference()) {
            stop();
            m_status = QStringLiteral("无法开启原生导入服务，请通过 rmtool 检查");
            emit changed();
            return;
        }
        m_importPrepared = true;
    }
    QFileInfo info(QString::fromLatin1(kState));
    if (!info.exists()) return;
    if (!info.isFile() || info.isSymLink() || info.ownerId() != 0 || info.size() > 4096
        || (info.permissions() & (QFile::ReadGroup | QFile::WriteGroup | QFile::ReadOther | QFile::WriteOther))) {
        stop();
        return;
    }
    QFile file(info.filePath());
    if (!file.open(QIODevice::ReadOnly)) { stop(); return; }
    const auto object = QJsonDocument::fromJson(file.read(4097)).object();
    const QUrl url(object.value(QStringLiteral("url")).toString());
    const auto expiry = QDateTime::fromString(object.value(QStringLiteral("expires_at")).toString(), Qt::ISODateWithMs);
    static const QRegularExpression token(QStringLiteral("^[A-Za-z0-9_-]{43}$"));
    if (url.adjusted(QUrl::RemoveFragment).toString() != m_origin + QStringLiteral("/")
        || !token.match(url.fragment()).hasMatch() || !expiry.isValid()
        || expiry <= QDateTime::currentDateTimeUtc()
        || expiry > QDateTime::currentDateTimeUtc().addSecs(16 * 60)) {
        stop();
        return;
    }
    const auto transfer = object.value(QStringLiteral("transfer")).toObject();
    const auto phase = transfer.value(QStringLiteral("phase")).toString();
    const auto filename = transfer.value(QStringLiteral("filename")).toString();
    auto status = transfer.value(QStringLiteral("message")).toString();
    const auto received = transfer.value(QStringLiteral("received")).toInteger(-1);
    const auto total = transfer.value(QStringLiteral("total")).toInteger(-1);
    const QStringList phases = {QStringLiteral("idle"), QStringLiteral("receiving"),
        QStringLiteral("validating"), QStringLiteral("importing"), QStringLiteral("done"), QStringLiteral("error")};
    if (!phases.contains(phase) || filename.toUtf8().size() > 240 || status.size() > 200
        || received < 0 || received > 200000000 || total < 0 || total > 200000000
        || (phase != QStringLiteral("error") && total > 0 && received > total)) { stop(); return; }
    QString detail;
    int progress = -1;
    if (phase == QStringLiteral("receiving")) {
        if (received > 0) status = QStringLiteral("正在接收文件");
        detail = QString::number(received / 1000000.0, 'f', 1) + QStringLiteral(" MB");
        if (total > 0) {
            progress = int(received * 100 / total);
            detail += QStringLiteral(" / ") + QString::number(total / 1000000.0, 'f', 1)
                + QStringLiteral(" MB (") + QString::number(progress) + QStringLiteral("%)");
        }
    }
    const auto expiryText = expiry.toLocalTime().toString(QStringLiteral("HH:mm"));
    if (m_url == url.toString() && m_expires == expiryText && m_status == status
        && m_filename == filename && m_transferDetail == detail && m_progress == progress) return;
    m_url = url.toString();
    m_expires = expiryText;
    m_status = status;
    m_filename = filename;
    m_transferDetail = detail;
    m_progress = progress;
    emit changed();
}

extern "C" void _xovi_construct()
{
    qmlRegisterType<QrUpload>("cn.rmtool.QrUpload", 1, 0, "QrUpload");
}

extern "C" char _xovi_shouldLoad()
{
    return dlsym(RTLD_DEFAULT, "_Z21qRegisterResourceDataiPKhS0_S0_") != nullptr;
}

extern "C" __attribute__((section(".xovi_info"))) const int EXTENSIONVERSION = 0x00000100;
__attribute__((section(".xovi"))) const char *LINKTABLENAMES = "Ephony\0\0";
__attribute__((section(".xovi"))) const void *LINKTABLEVALUES[] = {(void *)1, (void *)0};

#include "moc_QrUpload.cpp"
