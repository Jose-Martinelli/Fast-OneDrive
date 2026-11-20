// SPDX-License-Identifier: MIT
// Submenu "OneDrive" no botão direito do Dolphin para ficheiros em ~/OneDrive, como o
// menu do OneDrive no Explorer do Windows. Cada acção arranca o CLI fastonedrive em
// segundo plano; o CLI infere o perfil pela pasta da montagem.

#include "fastonedrivemounts.h"

#include <KAbstractFileItemActionPlugin>
#include <KFileItemListProperties>
#include <KPluginFactory>

#include <QAction>
#include <QIcon>
#include <QMenu>
#include <QProcess>
#include <QUrl>
#include <QWidget>

class FastOneDriveActionsPlugin : public KAbstractFileItemActionPlugin
{
    Q_OBJECT

public:
    FastOneDriveActionsPlugin(QObject *parent, const QVariantList &)
        : KAbstractFileItemActionPlugin(parent)
    {
    }

    QList<QAction *> actions(const KFileItemListProperties &fileItemInfos, QWidget *parentWidget) override
    {
        QStringList paths;
        const QList<QUrl> urls = fileItemInfos.urlList();
        for (const QUrl &url : urls) {
            if (!url.isLocalFile()) {
                return {};
            }
            const QString path = url.toLocalFile();
            if (!FastOneDrive::isUnderMount(path)) {
                return {};
            }
            paths.append(path);
        }
        if (paths.isEmpty()) {
            return {};
        }

        auto *menu = new QMenu(QStringLiteral("OneDrive"), parentWidget);
        menu->setIcon(QIcon::fromTheme(QStringLiteral("folder-cloud")));

        addAction(menu, QStringLiteral("Manter sempre neste dispositivo"), QStringLiteral("pin"), {QStringLiteral("pin"), QStringLiteral("--notify")}, paths);
        addAction(menu, QStringLiteral("Libertar espaço"), QStringLiteral("edit-clear"), {QStringLiteral("free-space"), QStringLiteral("--notify")}, paths);
        menu->addSeparator();
        addAction(menu, QStringLiteral("Partilhar ligação"), QStringLiteral("document-share"), {QStringLiteral("link"), QStringLiteral("--notify")}, paths);
        if (paths.size() == 1) {
            addAction(menu, QStringLiteral("Ver online"), QStringLiteral("internet-web-browser"), {QStringLiteral("open-web"), QStringLiteral("--notify")}, paths);
            addAction(menu, QStringLiteral("Histórico de versões…"), QStringLiteral("view-history"), {QStringLiteral("versions"), QStringLiteral("--notify")}, paths);
        }
        menu->addSeparator();
        addAction(menu, QStringLiteral("Pesquisar no OneDrive…"), QStringLiteral("search"), {QStringLiteral("dolphin-search")}, {});
        addAction(menu, QStringLiteral("Reciclagem do OneDrive"), QStringLiteral("user-trash"), {QStringLiteral("open-recycle")}, {});
        addAction(menu, QStringLiteral("Definições do Fast-OneDrive"), QStringLiteral("configure"), {QStringLiteral("settings")}, {});

        return {menu->menuAction()};
    }

private:
    void addAction(QMenu *menu, const QString &text, const QString &icon, const QStringList &args, const QStringList &paths)
    {
        QAction *action = menu->addAction(QIcon::fromTheme(icon), text);
        const QStringList full = args + paths;
        connect(action, &QAction::triggered, this, [full]() {
            QProcess::startDetached(FastOneDrive::cliBinary(), full);
        });
    }
};

K_PLUGIN_CLASS_WITH_JSON(FastOneDriveActionsPlugin, "fastonedriveactions.json")

#include "fastonedriveactions.moc"
