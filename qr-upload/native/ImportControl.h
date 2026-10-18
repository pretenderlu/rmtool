#pragma once

#include <QObject>
#include <QPointer>
#include <QLocalServer>
#include <QLocalSocket>

class ImportControl : public QObject
{
    Q_OBJECT
    Q_PROPERTY(QObject *settings READ settings WRITE setSettings)
public:
    explicit ImportControl(QObject *parent = nullptr);
    ~ImportControl() override;
    QObject *settings() const { return m_settings; }
    void setSettings(QObject *settings);
    static bool desktopActive();

private:
    void acceptConnection();
    bool restore();
    bool enable();
    void release();
    QPointer<QObject> m_settings;
    QPointer<QLocalSocket> m_client;
    QLocalServer m_server;
    bool m_enabled = false;
};
