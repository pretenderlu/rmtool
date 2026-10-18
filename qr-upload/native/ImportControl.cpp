#include "ImportControl.h"
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QSaveFile>
#include <QTimer>
#include <sys/socket.h>
#include <sys/stat.h>
#include <unistd.h>
#ifdef RMTOOL_NATIVE_IMPORT_STANDALONE
#include <dlfcn.h>
#endif

namespace {
constexpr auto kDir = "/run/rmtool-native-import-control";
constexpr auto kSocket = "/run/rmtool-native-import-control/control.sock";
constexpr auto kPreference = "/home/root/.rmtool-native-import-web-state";
constexpr auto kActive = "/run/rmtool-native-import-control/active";
constexpr char kDisabled[] = "rmtool-native-import:restore-web-disabled:v1\n";
ImportControl *control = nullptr;
}

ImportControl::ImportControl(QObject *parent) : QObject(parent)
{
    m_server.setMaxPendingConnections(1);
    m_server.setSocketOptions(QLocalServer::UserAccessOption);
    connect(&m_server, &QLocalServer::newConnection, this, &ImportControl::acceptConnection);
}

ImportControl::~ImportControl()
{
    release();
    m_server.close();
    if (control == this) {
        control = nullptr;
        QDir().rmdir(QString::fromLatin1(kDir));
    }
}

bool ImportControl::desktopActive()
{
    return control && !control->m_client.isNull();
}

void ImportControl::setSettings(QObject *settings)
{
    if (m_settings || control || !settings || !settings->property("webInterfaceEnabled").isValid()) return;
#ifdef RMTOOL_NATIVE_IMPORT_STANDALONE
    const QString root = QString::fromUtf8(qgetenv("XOVI_ROOT"));
    if (root != QStringLiteral("/data/rmtool/xovi-standalone")
        && root != QStringLiteral("/home/root/.local/share/rmtool/xovi-standalone")) return;
    // A loaded QR component already owns this same control protocol.
    const auto qrPath = (root + QStringLiteral("/extensions.d/rmtool-qr-upload.so")).toUtf8();
    // Xovi loads extensions RTLD_LOCAL, so query the existing handle only.
    if (void *qr = dlopen(qrPath.constData(), RTLD_NOW | RTLD_NOLOAD)) {
        const bool ownsControl = dlsym(qr, "_ZN13ImportControl16staticMetaObjectE") != nullptr;
        dlclose(qr);
        if (ownsControl) return;
    }
#endif
    m_settings = settings;
    control = this;
    if (!restore()) return;
    const QString path = QString::fromLatin1(kDir);
    if (::mkdir(kDir, 0700) != 0) return;
    if (::chmod(kDir, 0700) != 0 || !m_server.listen(QString::fromLatin1(kSocket))) {
        QDir().rmdir(path);
    }
}

bool ImportControl::restore()
{
    QFileInfo info(QString::fromLatin1(kPreference));
    if (!info.exists() && !info.isSymLink()) return true;
    if (!m_settings || !info.isFile() || info.isSymLink() || info.ownerId() != 0
        || info.size() > 64 || (info.permissions() & (QFile::ReadGroup | QFile::WriteGroup
            | QFile::ReadOther | QFile::WriteOther))) return false;
    QFile file(info.filePath());
    if (!file.open(QIODevice::ReadOnly) || file.read(65) != QByteArray(kDisabled)) return false;
    file.close();
    return m_settings->setProperty("webInterfaceEnabled", false) && QFile::remove(info.filePath());
}

bool ImportControl::enable()
{
    if (!m_settings || !restore()) return false;
    if (m_settings->property("webInterfaceEnabled").toBool()) return true;
    QSaveFile file(QString::fromLatin1(kPreference));
    file.setDirectWriteFallback(false);
    if (!file.open(QIODevice::WriteOnly) || !file.setPermissions(QFile::ReadOwner | QFile::WriteOwner)
        || file.write(kDisabled) != qint64(sizeof(kDisabled) - 1) || !file.commit()) return false;
    return m_settings->setProperty("webInterfaceEnabled", true);
}

void ImportControl::release()
{
    if (m_settings) restore();
    m_enabled = false;
    if (m_client) {
        m_client->disconnect(this);
        m_client->abort();
        m_client->deleteLater();
        m_client = nullptr;
        QFile::remove(QString::fromLatin1(kActive));
    }
}

void ImportControl::acceptConnection()
{
    while (auto *socket = m_server.nextPendingConnection()) {
        ucred peer{};
        socklen_t size = sizeof(peer);
        if (::getsockopt(socket->socketDescriptor(), SOL_SOCKET, SO_PEERCRED, &peer, &size) != 0
            || peer.uid != 0 || m_client
            || QFileInfo::exists(QStringLiteral("/run/rmtool-qr-upload-ui"))
            || QFileInfo::exists(QStringLiteral("/home/root/.rmtool-qr-upload-web-state"))) {
            socket->write("busy\n");
            connect(socket, &QLocalSocket::disconnected, socket, &QObject::deleteLater);
            socket->disconnectFromServer();
            continue;
        }
        m_client = socket;
        auto *deadline = new QTimer(socket);
        deadline->setSingleShot(true);
        deadline->setInterval(5000);
        connect(deadline, &QTimer::timeout, this, [this, socket]() {
            if (m_client == socket) release();
        });
        deadline->start();
        connect(socket, &QLocalSocket::disconnected, this, [this, socket]() {
            if (m_client == socket) release();
        });
        connect(socket, &QLocalSocket::readyRead, this, [this, socket, deadline]() {
            if (m_client != socket) return;
            if (socket->bytesAvailable() > 32) { release(); return; }
            if (!socket->canReadLine()) return;
            const auto command = socket->readLine(33);
            if (command == "acquire\n" && !m_enabled) {
                QSaveFile active(QString::fromLatin1(kActive));
                active.setDirectWriteFallback(false);
                if (!active.open(QIODevice::WriteOnly)
                    || !active.setPermissions(QFile::ReadOwner | QFile::WriteOwner)
                    || active.write("active\n") != 7 || !active.commit()) { release(); return; }
                deadline->stop();
                socket->write("reserved\n");
            } else if (command == "enable\n" && !m_enabled) {
                if (!enable()) { release(); return; }
                m_enabled = true;
                socket->write("ready\n");
            } else if (command == "release\n") {
                const bool restored = restore();
                m_enabled = false;
                socket->write(restored ? "restored\n" : "restore-failed\n");
                socket->disconnectFromServer();
            } else {
                release();
            }
        });
    }
}

#include "moc_ImportControl.cpp"
