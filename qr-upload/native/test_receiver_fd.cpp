// Linux/Qt integration probe. No device, receiver or privileged paths required.
#include <QCoreApplication>
#include <QCryptographicHash>
#include <QFile>
#include <QProcess>
#include <QPointer>
#include <QTemporaryDir>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <fcntl.h>
#include <memory>
#include <sys/stat.h>
#include <unistd.h>
#include "ReceiverProcess.h"

static void require(bool ok, const char *operation)
{
    if (!ok) { std::fprintf(stderr, "FAIL: %s\n", operation); std::exit(1); }
}

static void probe(const QString &path, bool executable)
{
    require(QFile::copy(QStringLiteral("/proc/self/exe"), path), "copy probe ELF");
    const auto name = QFile::encodeName(path);
    require(chmod(name.constData(), executable ? 0755 : 0644) == 0, "set probe mode");
    auto receiver = std::make_unique<QFile>();
    const int fd = open(name.constData(), O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC);
    require(fd > 2 && receiver->open(fd, QIODevice::ReadOnly, QFileDevice::AutoCloseHandle), "adopt descriptor");
    QCryptographicHash hash(QCryptographicHash::Sha256);
    require(hash.addData(receiver.get()), "read adopted ELF to EOF");
    struct stat st{};
    require(fstat(fd, &st) == 0 && (fcntl(fd, F_GETFD) & FD_CLOEXEC), "capture verified inode and CLOEXEC");
    require(QFile::remove(path), "unlink verified pathname");
    QFile replacement(path);
    require(replacement.open(QIODevice::WriteOnly) && replacement.write("not an executable") == 17, "replace original name");
    replacement.close();

    auto *process = new QProcess(QCoreApplication::instance());
    QPointer<QProcess> alive(process);
    const auto program = adoptVerifiedReceiver(process, std::move(receiver));
    process->start(program, {"child", QString::number(st.st_ino), QString::number(fd)});
    if (executable) {
        require(process->waitForStarted(), "QProcess exec of verified FD");
        require(fcntl(fd, F_GETFD) == -1 && errno == EBADF, "release parent FD once exec succeeds");
        require(process->waitForFinished() && process->exitStatus() == QProcess::NormalExit
                && process->exitCode() == 0 && process->readAllStandardOutput() == "verified child\n",
                "child ran original inode without inheriting receiver FD");
        process->deleteLater();
    } else {
        require(!process->waitForStarted() && process->error() == QProcess::FailedToStart, "refuse non-executable FD");
    }
    QCoreApplication::sendPostedEvents(nullptr, QEvent::DeferredDelete);
    require(!alive, "QProcess disposed without any page callback");
    require(fcntl(fd, F_GETFD) == -1 && errno == EBADF, "no FD after QProcess disposal");
    require(QFile::remove(path), "remove test replacement");
}

int main(int argc, char **argv)
{
    if (argc == 4 && QByteArray(argv[1]) == "child") {
        struct stat exe{}, inherited{};
        const int fd = std::atoi(argv[3]);
        require(stat("/proc/self/exe", &exe) == 0 && exe.st_ino == std::strtoull(argv[2], nullptr, 10), "exec inode");
        require(fstat(fd, &inherited) == -1 && errno == EBADF, "CLOEXEC in child");
        std::puts("verified child");
        return 0;
    }
    QCoreApplication app(argc, argv);
    QTemporaryDir dir;
    require(dir.isValid(), "create private test directory");
    const auto path = dir.filePath("receiver");
    probe(path, true);
    probe(path, false);
    std::printf("PASS Qt %s: replaced pathname, verified inode, CLOEXEC, parent close, failed start, FD cleanup\n", qVersion());
}
