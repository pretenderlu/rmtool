#pragma once

#include <QObject>
#include <QPointer>
#include <QProcess>
#include <QString>
#include <QTimer>

class QrUpload : public QObject
{
    Q_OBJECT
    Q_PROPERTY(bool busy READ busy NOTIFY changed)
    Q_PROPERTY(QString url READ url NOTIFY changed)
    Q_PROPERTY(QString status READ status NOTIFY changed)
    Q_PROPERTY(QString expires READ expires NOTIFY changed)
    Q_PROPERTY(QString filename READ filename NOTIFY changed)
    Q_PROPERTY(QString transferDetail READ transferDetail NOTIFY changed)
    Q_PROPERTY(int progress READ progress NOTIFY changed)
    Q_PROPERTY(QObject *settings READ settings WRITE setSettings)

public:
    explicit QrUpload(QObject *parent = nullptr);
    ~QrUpload() override;
    bool busy() const { return !m_process.isNull(); }
    QString url() const { return m_url; }
    QString status() const { return m_status; }
    QString expires() const { return m_expires; }
    QString filename() const { return m_filename; }
    QString transferDetail() const { return m_transferDetail; }
    int progress() const { return m_progress; }
    QObject *settings() const { return m_settings; }
    void setSettings(QObject *settings);
    Q_INVOKABLE void start();
    Q_INVOKABLE void stop();

signals:
    void changed();

private:
    void readSession();
    bool restoreImportPreference();
    bool enableImportPreference();
    QPointer<QObject> m_settings;
    QPointer<QProcess> m_process;
    QTimer m_poll;
    QString m_origin;
    QString m_url;
    QString m_expires;
    QString m_status;
    QString m_filename;
    QString m_transferDetail;
    int m_progress = -1;
    bool m_stopping = false;
    bool m_importPrepared = false;
};
