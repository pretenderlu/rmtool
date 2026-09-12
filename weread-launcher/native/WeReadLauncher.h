#pragma once

#include <QObject>
#include <QString>

class WeReadLauncher : public QObject
{
    Q_OBJECT

public:
    explicit WeReadLauncher(QObject *parent = nullptr) : QObject(parent) {}

    Q_INVOKABLE bool available() const;
    Q_INVOKABLE QString unavailableReason() const;
    Q_INVOKABLE bool launch(int mode, int refreshPages);
    Q_INVOKABLE QString lastError() const { return m_lastError; }

private:
    QString validateInstallation() const;
    QString validateFastMode() const;
    QString m_lastError;
};
