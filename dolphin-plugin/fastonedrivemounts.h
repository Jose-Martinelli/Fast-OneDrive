// SPDX-License-Identifier: LGPL-3.0-or-later
// Ajudas partilhadas pelos plugins do Dolphin: que pastas são montagens Fast-OneDrive
// e qual o estado de um caminho (atributo user.fastonedrive.state servido pelo FUSE).
#pragma once

#include <QElapsedTimer>
#include <QFile>
#include <QMutex>
#include <QMutexLocker>
#include <QStandardPaths>
#include <QString>
#include <QStringList>

#include <cerrno>
#include <sys/xattr.h>

namespace FastOneDrive
{

inline QString unescapeMountField(const QByteArray &field)
{
    // /proc/self/mountinfo escreve espaço, tab, \n e \ como \040, \011, \012, \134.
    QByteArray out;
    out.reserve(field.size());
    for (int i = 0; i < field.size(); ++i) {
        if (field.at(i) == '\\' && i + 3 < field.size()) {
            bool ok = false;
            const int value = field.mid(i + 1, 3).toInt(&ok, 8);
            if (ok) {
                out.append(static_cast<char>(value));
                i += 3;
                continue;
            }
        }
        out.append(field.at(i));
    }
    return QString::fromLocal8Bit(out);
}

inline QStringList readMounts()
{
    QStringList mounts;
    QFile file(QStringLiteral("/proc/self/mountinfo"));
    if (!file.open(QIODevice::ReadOnly)) {
        return mounts;
    }
    // Ficheiros de /proc reportam tamanho 0: QFile::atEnd() é logo verdadeiro e um
    // ciclo com readLine() não lê nada. readAll() lê até ao EOF.
    const QList<QByteArray> lines = file.readAll().split('\n');
    for (const QByteArray &line : lines) {
        const int dash = line.indexOf(" - ");
        if (dash < 0) {
            continue;
        }
        const QList<QByteArray> tail = line.mid(dash + 3).split(' ');
        if (tail.isEmpty() || tail.first() != "fuse.fastonedrive") {
            continue;
        }
        const QList<QByteArray> head = line.left(dash).split(' ');
        if (head.size() < 5) {
            continue;
        }
        mounts.append(unescapeMountField(head.at(4)));
    }
    return mounts;
}

inline QStringList mounts()
{
    static QMutex mutex;
    static QStringList cached;
    static QElapsedTimer age;
    QMutexLocker locker(&mutex);
    if (!age.isValid() || age.elapsed() > 5000) {
        cached = readMounts();
        age.restart();
    }
    return cached;
}

inline bool isUnderMount(const QString &path)
{
    const QStringList list = mounts();
    for (const QString &mount : list) {
        if (path == mount || path.startsWith(mount + QLatin1Char('/'))) {
            return true;
        }
    }
    return false;
}

// Pode bloquear se o FUSE estiver preso: chamar só fora da thread da interface.
inline QString readState(const QString &path)
{
    const QByteArray encoded = QFile::encodeName(path);
    char buffer[64];
    const ssize_t size = ::getxattr(encoded.constData(), "user.fastonedrive.state", buffer, sizeof(buffer) - 1);
    if (size < 0 && errno != ENODATA && errno != ENOENT) {
        // Falha passageira (FUSE ocupado, EIO): quem chama mantém o emblema que tinha.
        return QStringLiteral("?");
    }
    if (size <= 0) {
        return QString();
    }
    buffer[size] = '\0';
    return QString::fromLatin1(buffer, static_cast<int>(size));
}

inline QStringList overlaysForState(const QString &state)
{
    // Os mesmos emblemas que o worker KIO (kio_fastonedrive.cpp) mostra.
    if (state == QLatin1String("conflict")) {
        return {QStringLiteral("emblem-important")};
    }
    if (state == QLatin1String("error")) {
        return {QStringLiteral("emblem-error")};
    }
    if (state == QLatin1String("syncing")) {
        return {QStringLiteral("emblem-synchronizing")};
    }
    if (state == QLatin1String("pinned")) {
        return {QStringLiteral("emblem-checked")};
    }
    if (state == QLatin1String("cached")) {
        return {QStringLiteral("emblem-information")};
    }
    if (state == QLatin1String("cloud") || state == QLatin1String("partial")) {
        return {QStringLiteral("folder-cloud")};
    }
    return {};
}

inline QString cliBinary()
{
    const QByteArray fromEnv = qgetenv("FASTONEDRIVE_BIN");
    if (!fromEnv.isEmpty()) {
        return QString::fromLocal8Bit(fromEnv);
    }
    const QString local = QStandardPaths::writableLocation(QStandardPaths::HomeLocation) + QStringLiteral("/.local/bin/fastonedrive");
    if (QFile::exists(local)) {
        return local;
    }
    const QString found = QStandardPaths::findExecutable(QStringLiteral("fastonedrive"));
    return found.isEmpty() ? QStringLiteral("/usr/bin/fastonedrive") : found;
}

} // namespace FastOneDrive
