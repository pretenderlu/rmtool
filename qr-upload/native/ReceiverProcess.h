#pragma once

#include <QFile>
#include <QProcess>
#include <memory>

inline QString adoptVerifiedReceiver(QProcess *process, std::unique_ptr<QFile> receiver)
{
    const auto program = QStringLiteral("/proc/self/fd/%1").arg(receiver->handle());
    auto *file = receiver.release();
    file->setParent(process);
    QObject::connect(process, &QProcess::started, file, [file] { file->close(); });
    // Failed exec has no finished signal, and the settings page may be gone.
    QObject::connect(process, &QProcess::errorOccurred, process, [process](QProcess::ProcessError error) {
        if (error == QProcess::FailedToStart) process->deleteLater();
    });
    return program;
}
