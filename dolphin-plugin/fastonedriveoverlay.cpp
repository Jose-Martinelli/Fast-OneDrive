// SPDX-License-Identifier: MIT
// Emblemas de estado em ~/OneDrive (e em qualquer montagem fuse.fastonedrive).
//
// O Dolphin chama getOverlays() na thread da interface e pede que não bloqueie. O
// estado vem de um getxattr no FUSE, que pode bloquear se o FUSE estiver preso; por
// isso a leitura corre num QThreadPool e o resultado chega por overlaysChanged.
// O Fast-OneDrive emite org.fastonedrive.State.Changed(as) quando um item muda de
// estado; o plugin invalida esse caminho e os antepassados.

#include "fastonedrivemounts.h"

#include <KOverlayIconPlugin>

#include <QDBusConnection>
#include <QDateTime>
#include <QHash>
#include <QPointer>
#include <QSet>
#include <QThreadPool>
#include <QTimer>
#include <QUrl>

namespace
{
constexpr qint64 CacheTtlMs = 30000;
constexpr int CacheLimit = 50000;

struct CachedState {
    QString state;
    qint64 stamp = 0;
};

QString parentOf(const QString &path)
{
    const int slash = path.lastIndexOf(QLatin1Char('/'));
    if (slash <= 0) {
        return QString();
    }
    return path.left(slash);
}
}

class FastOneDriveOverlayPlugin : public KOverlayIconPlugin
{
    Q_OBJECT
    Q_PLUGIN_METADATA(IID "org.fastonedrive.overlayiconplugin" FILE "fastonedriveoverlay.json")

public:
    explicit FastOneDriveOverlayPlugin(QObject *parent = nullptr)
        : KOverlayIconPlugin(parent)
    {
        m_pool.setMaxThreadCount(2);
        // Rede de segurança para estados que mudam sozinhos (a sincronizar, a descarregar):
        // mesmo que um sinal D-Bus se perca, o emblema acerta em poucos segundos.
        m_refresh.setInterval(4000);
        connect(&m_refresh, &QTimer::timeout, this, &FastOneDriveOverlayPlugin::refreshTransient);
        m_refresh.start();
        QDBusConnection::sessionBus().connect(QString(),
                                              QStringLiteral("/org/fastonedrive/State"),
                                              QStringLiteral("org.fastonedrive.State"),
                                              QStringLiteral("Changed"),
                                              this,
                                              SLOT(onStateChanged(QStringList)));
    }

    ~FastOneDriveOverlayPlugin() override
    {
        m_pool.clear();
    }

    QStringList getOverlays(const QUrl &url) override
    {
        if (!url.isLocalFile()) {
            return {};
        }
        const QString path = url.toLocalFile();
        if (!FastOneDrive::isUnderMount(path)) {
            return {};
        }
        const auto found = m_cache.constFind(path);
        if (found != m_cache.constEnd()) {
            if (QDateTime::currentMSecsSinceEpoch() - found->stamp > CacheTtlMs) {
                query(path);
            }
            return FastOneDrive::overlaysForState(found->state);
        }
        query(path);
        return {};
    }

private Q_SLOTS:
    void onStateChanged(const QStringList &paths)
    {
        QSet<QString> refresh;
        for (const QString &path : paths) {
            QString current = path;
            while (!current.isEmpty() && FastOneDrive::isUnderMount(current)) {
                if (m_cache.contains(current)) {
                    refresh.insert(current);
                }
                current = parentOf(current);
            }
        }
        for (const QString &path : std::as_const(refresh)) {
            m_cache[path].stamp = 0;
            query(path);
        }
    }

    void refreshTransient()
    {
        const qint64 now = QDateTime::currentMSecsSinceEpoch();
        for (auto it = m_cache.cbegin(); it != m_cache.cend(); ++it) {
            const QString &state = it->state;
            const bool transient = state == QLatin1String("syncing") || state == QLatin1String("partial");
            if (transient || now - it->stamp > CacheTtlMs) {
                if (transient || m_pending.size() < 64) {
                    query(it.key());
                }
            }
        }
    }

private:
    void onResult(const QString &path, const QString &state)
    {
        m_pending.remove(path);
        if (state == QLatin1String("?")) {
            return;
        }
        if (state.isEmpty()) {
            // Sem estado (apagado, ou saiu do OneDrive): tirar o emblema antigo já, em
            // vez de o deixar até ao fim do TTL.
            if (m_cache.remove(path) > 0) {
                Q_EMIT overlaysChanged(QUrl::fromLocalFile(path), QStringList());
            }
            return;
        }
        if (m_cache.size() > CacheLimit) {
            m_cache.clear();
        }
        const auto found = m_cache.constFind(path);
        const bool changed = found == m_cache.constEnd() || found->state != state;
        m_cache.insert(path, CachedState{state, QDateTime::currentMSecsSinceEpoch()});
        if (changed) {
            Q_EMIT overlaysChanged(QUrl::fromLocalFile(path), FastOneDrive::overlaysForState(state));
        }
    }

    void query(const QString &path)
    {
        if (m_pending.contains(path)) {
            return;
        }
        m_pending.insert(path);
        QPointer<FastOneDriveOverlayPlugin> self(this);
        m_pool.start([self, path]() {
            const QString state = FastOneDrive::readState(path);
            if (self.isNull()) {
                return;
            }
            QMetaObject::invokeMethod(
                self.data(),
                [self, path, state]() {
                    if (!self.isNull()) {
                        self->onResult(path, state);
                    }
                },
                Qt::QueuedConnection);
        });
    }

    QThreadPool m_pool;
    QTimer m_refresh;
    QHash<QString, CachedState> m_cache;
    QSet<QString> m_pending;
};

#include "fastonedriveoverlay.moc"
