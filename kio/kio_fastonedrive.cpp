#include <KIO/UDSEntry>
#include <KIO/WorkerBase>

#include <QCoreApplication>
#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QHash>
#include <QIODevice>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QProcess>
#include <QProcessEnvironment>
#include <QSet>
#include <QTemporaryFile>
#include <QList>
#include <QUrl>

#include <signal.h>
#include <sys/stat.h>
#include <unistd.h>

namespace
{
const QString helpFileName = QStringLiteral("LEIA-ME.txt");
const QByteArray helpFileBody = QByteArrayLiteral(
	"Esta pasta mostra o OneDrive todo (índice SQLite), sem FUSE.\n"
	"Ctrl+C / Ctrl+V e arrastar usam rclone.\n"
	"Manter localmente / Deixar de manter só decide o que fica neste PC.\n"
	"Delete apaga na nuvem. Deixar de manter não apaga o OneDrive.\n"
	"\n"
	"Busca:\n"
	"  fastonedrive:/search/comemora\n"
	"  fastonedrive dolphin-search\n"
);

void collectDescendants(qint64 pid, QList<qint64> *pids)
{
	const QDir dir(QStringLiteral("/proc/%1/task").arg(pid));
	const QFileInfoList tasks = dir.entryInfoList(QDir::Dirs | QDir::NoDotAndDotDot);
	for (const QFileInfo &task : tasks) {
		QFile file(task.absoluteFilePath() + QStringLiteral("/children"));
		if (!file.open(QIODevice::ReadOnly | QIODevice::Text)) {
			continue;
		}
		const QByteArray data = file.readAll().simplified();
		for (const QByteArray &part : data.split(' ')) {
			bool ok = false;
			const qint64 child = part.toLongLong(&ok);
			if (!ok || child <= 1 || pids->contains(child)) {
				continue;
			}
			pids->append(child);
			collectDescendants(child, pids);
		}
	}
}

bool fuseMountReady()
{
	QFile file(QStringLiteral("/proc/mounts"));
	if (!file.open(QIODevice::ReadOnly | QIODevice::Text)) {
		return false;
	}
	const QString mountPoint = QDir::homePath() + QStringLiteral("/OneDrive");
	const QList<QByteArray> lines = file.readAll().split('\n');
	for (const QByteArray &line : lines) {
		const QList<QByteArray> parts = line.split(' ');
		if (parts.size() < 3) {
			continue;
		}
		if (QString::fromUtf8(parts.at(1)) == mountPoint && parts.at(2).startsWith("fuse")) {
			return true;
		}
	}
	return false;
}

QString fuseItemPath(const QString &remotePath)
{
	return QDir::homePath() + QStringLiteral("/OneDrive/") + remotePath;
}

bool isLauncherTemp(const QString &destPath)
{
	const QString temp = QDir::tempPath();
	if (destPath.startsWith(temp + QLatin1Char('/'))) {
		return true;
	}
	return destPath.contains(QStringLiteral("/kioexec"));
}

bool placeStagingOver(const QString &staging, const QString &destPath)
{
	const QFileInfo destInfo(destPath);
	const QString backup = destInfo.absolutePath()
		+ QStringLiteral("/.fod-bak-")
		+ destInfo.fileName();
	if (QFileInfo(backup).isDir() && !QFileInfo(backup).isSymLink()) {
		QDir(backup).removeRecursively();
	} else {
		QFile::remove(backup);
	}
	const bool destPresent = destInfo.exists() || destInfo.isSymLink();
	if (destPresent && !QFile::rename(destPath, backup)) {
		return false;
	}
	if (!QFile::rename(staging, destPath)) {
		if (destPresent) {
			QFile::rename(backup, destPath);
		}
		return false;
	}
	if (QFileInfo(backup).isDir() && !QFileInfo(backup).isSymLink()) {
		QDir(backup).removeRecursively();
	} else {
		QFile::remove(backup);
	}
	return true;
}

void killProcessTree(qint64 pid)
{
	if (pid <= 1) {
		return;
	}
	QList<qint64> pids;
	pids.append(pid);
	collectDescendants(pid, &pids);
	for (int i = pids.size() - 1; i >= 0; --i) {
		::kill(static_cast<pid_t>(pids.at(i)), SIGTERM);
	}
	usleep(200000);
	for (int i = pids.size() - 1; i >= 0; --i) {
		::kill(static_cast<pid_t>(pids.at(i)), SIGKILL);
	}
}
}

class FastOneDriveWorker : public KIO::WorkerBase
{
public:
	FastOneDriveWorker(const QByteArray &pool, const QByteArray &app);
	KIO::WorkerResult listDir(const QUrl &url) override;
	KIO::WorkerResult stat(const QUrl &url) override;
	KIO::WorkerResult mimetype(const QUrl &url) override;
	KIO::WorkerResult get(const QUrl &url) override;
	KIO::WorkerResult put(const QUrl &url, int permissions, KIO::JobFlags flags) override;
	KIO::WorkerResult mkdir(const QUrl &url, int permissions) override;
	KIO::WorkerResult del(const QUrl &url, bool isfile) override;
	KIO::WorkerResult rename(const QUrl &src, const QUrl &dest, KIO::JobFlags flags) override;
	KIO::WorkerResult copy(const QUrl &src, const QUrl &dest, int permissions, KIO::JobFlags flags) override;
	KIO::WorkerResult chmod(const QUrl &url, int permissions) override;

private:
	QString cliBin() const;
	QString takeProfile(const QString &path);
	QStringList withProfile(const QStringList &args) const;
	QString normalizedPath(const QUrl &url);
	QString searchQuery(const QString &path) const;
	QString itemPath(const QString &path) const;
	QUrl itemUrl(const QString &itemPath) const;
	KIO::UDSEntry helpEntry() const;
	KIO::UDSEntry dirEntry(const QString &name, const QUrl &url) const;
	KIO::UDSEntry entryFromJson(const QJsonObject &object) const;
	QJsonArray runJsonArray(const QStringList &args, QString *errorOut) const;
	QJsonObject runJsonObject(const QStringList &args, QString *errorOut) const;
	QByteArray runProcess(const QString &binary, const QStringList &args, QString *errorOut, int *exitCode) const;
	void emitHelpFile();
	void appendLog(const QString &message) const;
	bool isPreviewRequest() const;
	KIO::WorkerResult failFromCli(const QString &errorText) const;
	KIO::WorkerResult redirectLocalFile(const QString &localPath);
	KIO::WorkerResult copyLocalPath(const QString &localPath, const QString &destPath, KIO::JobFlags flags);
	KIO::WorkerResult runCli(const QStringList &args);
	KIO::WorkerResult runCliProgress(
		const QStringList &args,
		KIO::filesize_t knownTotal,
		const QString &localDest = QString(),
		QString *plainOut = nullptr
	);
	void flushProgress(QByteArray *buffer, KIO::filesize_t knownTotal, QString *plainOut);
	KIO::filesize_t partialBytes(const QString &destPath) const;
	QString itemKioUrl(const QString &remotePath) const;
	QString itemKioUrlFor(const QString &profileId, const QString &remotePath) const;
	bool isProtectedPath(const QString &path) const;
	KIO::WorkerResult refuseIfRemoteExists(const QString &remotePath, KIO::JobFlags flags, const QUrl &url);
	void cacheListing(const QJsonArray &children);
	QJsonObject cachedItem(const QString &remotePath) const;

	QHash<QString, QJsonObject> m_itemCache;
	QString m_profileId;
};

FastOneDriveWorker::FastOneDriveWorker(const QByteArray &pool, const QByteArray &app)
	: KIO::WorkerBase("fastonedrive", pool, app)
{
}

QString FastOneDriveWorker::cliBin() const
{
	const QByteArray env = qgetenv("FASTONEDRIVE_BIN");
	if (!env.isEmpty()) {
		return QString::fromLocal8Bit(env);
	}
	return QDir::homePath() + QStringLiteral("/.local/bin/fastonedrive");
}

QString FastOneDriveWorker::takeProfile(const QString &path)
{
	m_profileId.clear();
	if (!path.startsWith(QStringLiteral("/p/"))) {
		return path;
	}
	const QString body = path.mid(3);
	const int slash = body.indexOf(QLatin1Char('/'));
	const QString id = slash < 0 ? body : body.left(slash);
	if (id.isEmpty()) {
		return path;
	}
	for (const QChar &ch : id) {
		const char latin = ch.toLatin1();
		const bool ok = (latin >= 'a' && latin <= 'z')
			|| (latin >= '0' && latin <= '9')
			|| latin == '-';
		if (!ok) {
			return path;
		}
	}
	m_profileId = id;
	if (slash < 0 || slash >= body.size() - 1) {
		return QStringLiteral("/");
	}
	return body.mid(slash);
}

QStringList FastOneDriveWorker::withProfile(const QStringList &args) const
{
	if (m_profileId.isEmpty()) {
		return args;
	}
	QStringList command;
	command << QStringLiteral("--profile") << m_profileId;
	command << args;
	return command;
}

QString FastOneDriveWorker::normalizedPath(const QUrl &url)
{
	QString path = url.adjusted(QUrl::StripTrailingSlash).path();
	if (path.isEmpty()) {
		path = QStringLiteral("/");
	}
	path = takeProfile(path);
	if (path.isEmpty()) {
		return QStringLiteral("/");
	}
	return path;
}

QString FastOneDriveWorker::searchQuery(const QString &path) const
{
	const QString prefix = QStringLiteral("/search/");
	if (!path.startsWith(prefix)) {
		return QString();
	}
	return path.mid(prefix.size());
}

QString FastOneDriveWorker::itemPath(const QString &path) const
{
	if (path == QStringLiteral("/search") || path.startsWith(QStringLiteral("/search/"))) {
		return QString();
	}
	if (path == QStringLiteral("/") + helpFileName) {
		return QString();
	}
	if (path == QStringLiteral("/") || path == QStringLiteral("/item")) {
		return QString();
	}
	const QString prefix = QStringLiteral("/item/");
	if (path.startsWith(prefix)) {
		return path.mid(prefix.size());
	}
	if (path.startsWith(QLatin1Char('/'))) {
		return path.mid(1);
	}
	return path;
}

QString FastOneDriveWorker::itemKioUrl(const QString &remotePath) const
{
	return itemKioUrlFor(m_profileId, remotePath);
}

QString FastOneDriveWorker::itemKioUrlFor(const QString &profileId, const QString &remotePath) const
{
	if (profileId.isEmpty()) {
		return QStringLiteral("fastonedrive:/item/") + remotePath;
	}
	return QStringLiteral("fastonedrive:/p/") + profileId + QStringLiteral("/item/") + remotePath;
}

bool FastOneDriveWorker::isProtectedPath(const QString &path) const
{
	return path == QStringLiteral("/")
		|| path == QStringLiteral("/item")
		|| path == QStringLiteral("/") + helpFileName
		|| path == QStringLiteral("/search")
		|| path.startsWith(QStringLiteral("/search/"));
}

QUrl FastOneDriveWorker::itemUrl(const QString &remotePath) const
{
	QUrl url;
	url.setScheme(QStringLiteral("fastonedrive"));
	if (m_profileId.isEmpty()) {
		url.setPath(QStringLiteral("/item/") + remotePath);
	} else {
		url.setPath(QStringLiteral("/p/") + m_profileId + QStringLiteral("/item/") + remotePath);
	}
	return url;
}

KIO::UDSEntry FastOneDriveWorker::helpEntry() const
{
	KIO::UDSEntry entry;
	entry.fastInsert(KIO::UDSEntry::UDS_NAME, helpFileName);
	entry.fastInsert(KIO::UDSEntry::UDS_DISPLAY_NAME, helpFileName);
	entry.fastInsert(KIO::UDSEntry::UDS_FILE_TYPE, S_IFREG);
	entry.fastInsert(KIO::UDSEntry::UDS_ACCESS, 0444);
	entry.fastInsert(KIO::UDSEntry::UDS_SIZE, static_cast<long long>(helpFileBody.size()));
	entry.fastInsert(KIO::UDSEntry::UDS_MIME_TYPE, QStringLiteral("text/plain"));
	QUrl url;
	url.setScheme(QStringLiteral("fastonedrive"));
	if (m_profileId.isEmpty()) {
		url.setPath(QStringLiteral("/") + helpFileName);
	} else {
		url.setPath(QStringLiteral("/p/") + m_profileId + QStringLiteral("/") + helpFileName);
	}
	entry.fastInsert(KIO::UDSEntry::UDS_URL, url.toString());
	return entry;
}

KIO::UDSEntry FastOneDriveWorker::dirEntry(const QString &name, const QUrl &url) const
{
	KIO::UDSEntry entry;
	entry.fastInsert(KIO::UDSEntry::UDS_NAME, name);
	entry.fastInsert(KIO::UDSEntry::UDS_DISPLAY_NAME, name);
	entry.fastInsert(KIO::UDSEntry::UDS_FILE_TYPE, S_IFDIR);
	entry.fastInsert(KIO::UDSEntry::UDS_ACCESS, 0755);
	entry.fastInsert(KIO::UDSEntry::UDS_MIME_TYPE, QStringLiteral("inode/directory"));
	entry.fastInsert(KIO::UDSEntry::UDS_URL, url.toString());
	return entry;
}

KIO::UDSEntry FastOneDriveWorker::entryFromJson(const QJsonObject &object) const
{
	const QString remotePath = object.value(QStringLiteral("path")).toString();
	const QString name = object.value(QStringLiteral("name")).toString();
	const bool isDir = object.value(QStringLiteral("isDir")).toBool();
	const qint64 size = object.value(QStringLiteral("size")).toVariant().toLongLong();
	QString mime = object.value(QStringLiteral("mimeType")).toString();
	const bool pinned = object.value(QStringLiteral("pinned")).toBool();
	const QString localPath = object.value(QStringLiteral("localPath")).toString();
	const QString status = object.value(QStringLiteral("status")).toString();
	int access = isDir ? 0755 : (localPath.isEmpty() ? 0444 : 0644);
	if (object.contains(QStringLiteral("access"))) {
		access = object.value(QStringLiteral("access")).toInt() & 0777;
	}
	KIO::UDSEntry entry;
	entry.fastInsert(KIO::UDSEntry::UDS_NAME, name);
	entry.fastInsert(KIO::UDSEntry::UDS_DISPLAY_NAME, name);
	entry.fastInsert(KIO::UDSEntry::UDS_FILE_TYPE, isDir ? S_IFDIR : S_IFREG);
	entry.fastInsert(KIO::UDSEntry::UDS_ACCESS, access);
	if (status == QStringLiteral("conflict")) {
		entry.fastInsert(KIO::UDSEntry::UDS_COMMENT, QStringLiteral("Conflito de sincronização"));
		entry.fastInsert(KIO::UDSEntry::UDS_ICON_OVERLAY_NAMES, QStringLiteral("emblem-important"));
	} else if (status == QStringLiteral("error")) {
		entry.fastInsert(KIO::UDSEntry::UDS_COMMENT, QStringLiteral("Erro de sincronização"));
		entry.fastInsert(KIO::UDSEntry::UDS_ICON_OVERLAY_NAMES, QStringLiteral("emblem-error"));
	} else if (status == QStringLiteral("syncing")) {
		entry.fastInsert(KIO::UDSEntry::UDS_COMMENT, QStringLiteral("A sincronizar"));
		entry.fastInsert(KIO::UDSEntry::UDS_ICON_OVERLAY_NAMES, QStringLiteral("emblem-synchronizing"));
	} else if (status == QStringLiteral("pinned") || pinned) {
		entry.fastInsert(KIO::UDSEntry::UDS_COMMENT, QStringLiteral("Mantido neste PC"));
		entry.fastInsert(KIO::UDSEntry::UDS_ICON_OVERLAY_NAMES, QStringLiteral("emblem-checked"));
	} else if (status == QStringLiteral("cached") || !localPath.isEmpty()) {
		entry.fastInsert(KIO::UDSEntry::UDS_COMMENT, QStringLiteral("Em cache"));
		entry.fastInsert(KIO::UDSEntry::UDS_ICON_OVERLAY_NAMES, QStringLiteral("emblem-information"));
	} else {
		entry.fastInsert(KIO::UDSEntry::UDS_COMMENT, QStringLiteral("Só na nuvem"));
		entry.fastInsert(KIO::UDSEntry::UDS_ICON_OVERLAY_NAMES, QStringLiteral("folder-cloud"));
	}
	entry.fastInsert(KIO::UDSEntry::UDS_SIZE, isDir ? static_cast<long long>(0) : size);
	if (isDir) {
		entry.fastInsert(
			KIO::UDSEntry::UDS_MIME_TYPE,
			mime.isEmpty() ? QStringLiteral("inode/directory") : mime
		);
	} else if (!mime.isEmpty()) {
		entry.fastInsert(KIO::UDSEntry::UDS_MIME_TYPE, mime);
	}
	if (!localPath.isEmpty()) {
		entry.fastInsert(KIO::UDSEntry::UDS_LOCAL_PATH, localPath);
	}
	entry.fastInsert(KIO::UDSEntry::UDS_URL, itemUrl(remotePath).toString());
	const QString modTime = object.value(QStringLiteral("modTime")).toString();
	const QDateTime dateTime = QDateTime::fromString(modTime, Qt::ISODate);
	if (dateTime.isValid()) {
		entry.fastInsert(KIO::UDSEntry::UDS_MODIFICATION_TIME, dateTime.toSecsSinceEpoch());
	}
	return entry;
}

QByteArray FastOneDriveWorker::runProcess(
	const QString &binary,
	const QStringList &args,
	QString *errorOut,
	int *exitCode
) const
{
	QProcess process;
	process.setProcessChannelMode(QProcess::SeparateChannels);
	process.start(binary, args);
	if (!process.waitForFinished(-1)) {
		if (errorOut != nullptr) {
			*errorOut = QStringLiteral("falha ao executar %1").arg(binary);
		}
		if (exitCode != nullptr) {
			*exitCode = 1;
		}
		return QByteArray();
	}
	if (exitCode != nullptr) {
		*exitCode = process.exitCode();
	}
	const QByteArray stdoutBytes = process.readAllStandardOutput();
	const QString stderrText = QString::fromUtf8(process.readAllStandardError()).trimmed();
	if (process.exitCode() != 0) {
		if (errorOut != nullptr) {
			*errorOut = stderrText.isEmpty()
				? QStringLiteral("%1 saiu com código %2").arg(binary).arg(process.exitCode())
				: stderrText;
		}
		return QByteArray();
	}
	return stdoutBytes;
}

QJsonArray FastOneDriveWorker::runJsonArray(const QStringList &args, QString *errorOut) const
{
	int exitCode = 1;
	const QByteArray output = runProcess(cliBin(), withProfile(args), errorOut, &exitCode);
	if (exitCode != 0) {
		return QJsonArray();
	}
	const QJsonDocument document = QJsonDocument::fromJson(output);
	if (!document.isArray()) {
		if (errorOut != nullptr) {
			*errorOut = QStringLiteral("saída JSON inválida do CLI");
		}
		return QJsonArray();
	}
	return document.array();
}

QJsonObject FastOneDriveWorker::runJsonObject(const QStringList &args, QString *errorOut) const
{
	int exitCode = 1;
	const QByteArray output = runProcess(cliBin(), withProfile(args), errorOut, &exitCode);
	if (exitCode != 0) {
		return QJsonObject();
	}
	const QJsonDocument document = QJsonDocument::fromJson(output);
	if (!document.isObject()) {
		if (errorOut != nullptr) {
			*errorOut = QStringLiteral("saída JSON inválida do CLI");
		}
		return QJsonObject();
	}
	return document.object();
}

void FastOneDriveWorker::appendLog(const QString &message) const
{
	const QString path = QDir::homePath() + QStringLiteral("/.local/share/fast-onedrive/fastonedrive.log");
	QDir().mkpath(QFileInfo(path).absolutePath());
	QFile file(path);
	if (!file.open(QIODevice::WriteOnly | QIODevice::Append | QIODevice::Text)) {
		return;
	}
	const QString line = QDateTime::currentDateTime().toString(QStringLiteral("yyyy-MM-dd HH:mm:ss"))
		+ QStringLiteral(" ERROR kio  ")
		+ message
		+ QLatin1Char('\n');
	file.write(line.toUtf8());
}

KIO::WorkerResult FastOneDriveWorker::failFromCli(const QString &errorText) const
{
	const QString message = errorText.isEmpty()
		? QStringLiteral("erro no Fast-OneDrive")
		: errorText;
	appendLog(message);
	return KIO::WorkerResult::fail(KIO::ERR_WORKER_DEFINED, message);
}

void FastOneDriveWorker::cacheListing(const QJsonArray &children)
{
	for (const QJsonValue &value : children) {
		const QJsonObject object = value.toObject();
		const QString remotePath = object.value(QStringLiteral("path")).toString();
		if (!remotePath.isEmpty()) {
			m_itemCache.insert(remotePath, object);
		}
	}
}

QJsonObject FastOneDriveWorker::cachedItem(const QString &remotePath) const
{
	return m_itemCache.value(remotePath);
}

void FastOneDriveWorker::emitHelpFile()
{
	mimeType(QStringLiteral("text/plain"));
	totalSize(static_cast<KIO::filesize_t>(helpFileBody.size()));
	data(helpFileBody);
	data(QByteArray());
}

KIO::WorkerResult FastOneDriveWorker::listDir(const QUrl &url)
{
	const QString path = normalizedPath(url);
	if (path == QStringLiteral("/") || path == QStringLiteral("/item")) {
		QString errorText;
		const QJsonArray children = runJsonArray(
			QStringList() << QStringLiteral("children"),
			&errorText
		);
		if (!errorText.isEmpty()) {
			return failFromCli(errorText);
		}
		cacheListing(children);
		listEntry(helpEntry());
		QSet<QString> usedNames;
		usedNames.insert(helpFileName);
		for (const QJsonValue &value : children) {
			KIO::UDSEntry entry = entryFromJson(value.toObject());
			QString name = entry.stringValue(KIO::UDSEntry::UDS_NAME);
			int suffix = 2;
			while (usedNames.contains(name)) {
				name = entry.stringValue(KIO::UDSEntry::UDS_DISPLAY_NAME)
					+ QStringLiteral(" (")
					+ QString::number(suffix)
					+ QStringLiteral(")");
				suffix += 1;
			}
			entry.replace(KIO::UDSEntry::UDS_NAME, name);
			entry.replace(KIO::UDSEntry::UDS_DISPLAY_NAME, name);
			usedNames.insert(name);
			listEntry(entry);
		}
		return KIO::WorkerResult::pass();
	}
	if (path == QStringLiteral("/search") || path.startsWith(QStringLiteral("/search/"))) {
		const QString query = searchQuery(path);
		if (query.isEmpty()) {
			return KIO::WorkerResult::fail(
				KIO::ERR_WORKER_DEFINED,
				QStringLiteral("busca vazia; use fastonedrive:/search/termo")
			);
		}
		QString errorText;
		const QJsonArray hits = runJsonArray(
			QStringList()
				<< QStringLiteral("search")
				<< QStringLiteral("--json")
				<< QStringLiteral("--limit")
				<< QStringLiteral("200")
				<< query,
			&errorText
		);
		if (!errorText.isEmpty()) {
			return failFromCli(errorText);
		}
		cacheListing(hits);
		QSet<QString> usedNames;
		for (const QJsonValue &value : hits) {
			KIO::UDSEntry entry = entryFromJson(value.toObject());
			QString name = entry.stringValue(KIO::UDSEntry::UDS_NAME);
			int suffix = 2;
			while (usedNames.contains(name)) {
				name = entry.stringValue(KIO::UDSEntry::UDS_DISPLAY_NAME)
					+ QStringLiteral(" (")
					+ QString::number(suffix)
					+ QStringLiteral(")");
				suffix += 1;
			}
			entry.replace(KIO::UDSEntry::UDS_NAME, name);
			entry.replace(KIO::UDSEntry::UDS_DISPLAY_NAME, name);
			usedNames.insert(name);
			listEntry(entry);
		}
		return KIO::WorkerResult::pass();
	}
	const QString remotePath = itemPath(path);
	if (remotePath.isEmpty()) {
		return KIO::WorkerResult::fail(KIO::ERR_CANNOT_ENTER_DIRECTORY, url.toDisplayString());
	}
	QString errorText;
	const QJsonArray children = runJsonArray(
		QStringList() << QStringLiteral("children") << remotePath,
		&errorText
	);
	if (!errorText.isEmpty()) {
		return failFromCli(errorText);
	}
	cacheListing(children);
	if (children.isEmpty()) {
		const QJsonObject item = runJsonObject(
			QStringList() << QStringLiteral("item") << remotePath,
			&errorText
		);
		if (!item.isEmpty() && !item.value(QStringLiteral("isDir")).toBool()) {
			return KIO::WorkerResult::fail(KIO::ERR_IS_FILE, url.toDisplayString());
		}
		if (item.isEmpty()) {
			return KIO::WorkerResult::fail(KIO::ERR_CANNOT_ENTER_DIRECTORY, url.toDisplayString());
		}
	}
	QSet<QString> usedNames;
	for (const QJsonValue &value : children) {
		KIO::UDSEntry entry = entryFromJson(value.toObject());
		QString name = entry.stringValue(KIO::UDSEntry::UDS_NAME);
		int suffix = 2;
		while (usedNames.contains(name)) {
			name = entry.stringValue(KIO::UDSEntry::UDS_DISPLAY_NAME)
				+ QStringLiteral(" (")
				+ QString::number(suffix)
				+ QStringLiteral(")");
			suffix += 1;
		}
		entry.replace(KIO::UDSEntry::UDS_NAME, name);
		entry.replace(KIO::UDSEntry::UDS_DISPLAY_NAME, name);
		usedNames.insert(name);
		listEntry(entry);
	}
	return KIO::WorkerResult::pass();
}

bool isMissingItemError(const QString &errorText)
{
	const QString lower = errorText.toLower();
	return lower.contains(QStringLiteral("não encontrado"))
		|| lower.contains(QStringLiteral("not found"));
}

KIO::WorkerResult FastOneDriveWorker::stat(const QUrl &url)
{
	const QString path = normalizedPath(url);
	if (path == QStringLiteral("/") || path == QStringLiteral("/item")) {
		statEntry(dirEntry(QStringLiteral("OneDrive"), url));
		return KIO::WorkerResult::pass();
	}
	if (path == QStringLiteral("/") + helpFileName) {
		statEntry(helpEntry());
		return KIO::WorkerResult::pass();
	}
	if (path == QStringLiteral("/search") || path.startsWith(QStringLiteral("/search/"))) {
		const QString query = searchQuery(path);
		const QString name = query.isEmpty() ? QStringLiteral("search") : query;
		statEntry(dirEntry(name, url));
		return KIO::WorkerResult::pass();
	}
	const QString remotePath = itemPath(path);
	if (remotePath.isEmpty()) {
		return KIO::WorkerResult::fail(KIO::ERR_DOES_NOT_EXIST, url.toDisplayString());
	}
	QJsonObject cached = cachedItem(remotePath);
	if (!cached.isEmpty()) {
		statEntry(entryFromJson(cached));
		return KIO::WorkerResult::pass();
	}
	QString errorText;
	const QJsonObject item = runJsonObject(
		QStringList() << QStringLiteral("item") << remotePath,
		&errorText
	);
	if (!item.isEmpty()) {
		m_itemCache.insert(remotePath, item);
		statEntry(entryFromJson(item));
		return KIO::WorkerResult::pass();
	}
	if (isMissingItemError(errorText)) {
		return KIO::WorkerResult::fail(KIO::ERR_DOES_NOT_EXIST, url.toDisplayString());
	}
	errorText.clear();
	const QJsonArray children = runJsonArray(
		QStringList() << QStringLiteral("children") << remotePath,
		&errorText
	);
	if (children.isEmpty()) {
		return failFromCli(errorText);
	}
	statEntry(dirEntry(remotePath.section(QLatin1Char('/'), -1), url));
	return KIO::WorkerResult::pass();
}

KIO::WorkerResult FastOneDriveWorker::mimetype(const QUrl &url)
{
	const QString path = normalizedPath(url);
	if (path == QStringLiteral("/") || path == QStringLiteral("/item")
		|| path == QStringLiteral("/search") || path.startsWith(QStringLiteral("/search/"))) {
		mimeType(QStringLiteral("inode/directory"));
		return KIO::WorkerResult::pass();
	}
	if (path == QStringLiteral("/") + helpFileName) {
		mimeType(QStringLiteral("text/plain"));
		return KIO::WorkerResult::pass();
	}
	const QString remotePath = itemPath(path);
	if (remotePath.isEmpty()) {
		return KIO::WorkerResult::fail(KIO::ERR_DOES_NOT_EXIST, url.toDisplayString());
	}
	QJsonObject item = cachedItem(remotePath);
	QString errorText;
	if (item.isEmpty()) {
		item = runJsonObject(
			QStringList() << QStringLiteral("item") << remotePath,
			&errorText
		);
		if (!item.isEmpty()) {
			m_itemCache.insert(remotePath, item);
		}
	}
	if (item.isEmpty()) {
		return failFromCli(errorText);
	}
	QString mime = item.value(QStringLiteral("mimeType")).toString();
	if (mime.isEmpty()) {
		mime = item.value(QStringLiteral("isDir")).toBool()
			? QStringLiteral("inode/directory")
			: QStringLiteral("application/octet-stream");
	}
	mimeType(mime);
	return KIO::WorkerResult::pass();
}

KIO::WorkerResult FastOneDriveWorker::get(const QUrl &url)
{
	const QString path = normalizedPath(url);
	if (path == QStringLiteral("/") + helpFileName) {
		emitHelpFile();
		return KIO::WorkerResult::pass();
	}
	const QString remotePath = itemPath(path);
	if (remotePath.isEmpty()) {
		return KIO::WorkerResult::fail(
			KIO::ERR_CANNOT_OPEN_FOR_READING,
			url.toDisplayString()
		);
	}
	QJsonObject item = cachedItem(remotePath);
	QString errorText;
	if (item.isEmpty()) {
		item = runJsonObject(
			QStringList() << QStringLiteral("item") << remotePath,
			&errorText
		);
		if (!item.isEmpty()) {
			m_itemCache.insert(remotePath, item);
		}
	}
	if (item.isEmpty()) {
		return failFromCli(errorText);
	}
	if (item.value(QStringLiteral("isDir")).toBool()) {
		return KIO::WorkerResult::fail(KIO::ERR_IS_DIRECTORY, url.toDisplayString());
	}
	const QString localPath = item.value(QStringLiteral("localPath")).toString();
	if (!localPath.isEmpty()) {
		return redirectLocalFile(localPath);
	}
	if (isPreviewRequest()) {
		return KIO::WorkerResult::fail(
			KIO::ERR_CANNOT_OPEN_FOR_READING,
			url.toDisplayString()
		);
	}
	if (item.value(QStringLiteral("tooLargeToOpen")).toBool()) {
		const QString fusePath = item.value(QStringLiteral("fusePath")).toString();
		const bool ready = item.contains(QStringLiteral("fuseReady"))
			? item.value(QStringLiteral("fuseReady")).toBool()
			: fuseMountReady();
		if (!ready || (fusePath.isEmpty() && !fuseMountReady())) {
			return KIO::WorkerResult::fail(
				KIO::ERR_CANNOT_OPEN_FOR_READING,
				QStringLiteral("Activa o FUSE do Fast-OneDrive para abrir este ficheiro.")
			);
		}
		return redirectLocalFile(fusePath.isEmpty() ? fuseItemPath(remotePath) : fusePath);
	}
	int exitCode = 1;
	const QByteArray body = runProcess(
		cliBin(),
		withProfile(
			QStringList()
				<< QStringLiteral("hydrate")
				<< itemKioUrl(remotePath)
		),
		&errorText,
		&exitCode
	);
	if (exitCode != 0) {
		return failFromCli(errorText);
	}
	const QString hydrated = QString::fromUtf8(body).trimmed();
	if (hydrated.isEmpty()) {
		return failFromCli(QStringLiteral("hydrate não devolveu caminho local"));
	}
	return redirectLocalFile(hydrated);
}

KIO::WorkerResult FastOneDriveWorker::copy(
	const QUrl &src,
	const QUrl &dest,
	int permissions,
	KIO::JobFlags flags
)
{
	Q_UNUSED(permissions);
	if (src.scheme() == QStringLiteral("file") && dest.scheme() == QStringLiteral("fastonedrive")) {
		if (isProtectedPath(normalizedPath(dest))) {
			return KIO::WorkerResult::fail(KIO::ERR_CANNOT_WRITE, dest.toDisplayString());
		}
		const QString destItem = itemPath(normalizedPath(dest));
		if (destItem.isEmpty()) {
			return KIO::WorkerResult::fail(KIO::ERR_CANNOT_WRITE, dest.toDisplayString());
		}
		const QString localPath = src.toLocalFile();
		if (localPath.isEmpty()) {
			return KIO::WorkerResult::fail(KIO::ERR_MALFORMED_URL, src.toDisplayString());
		}
		const KIO::WorkerResult existsCheck = refuseIfRemoteExists(destItem, flags, dest);
		if (!existsCheck.success()) {
			return existsCheck;
		}
		QStringList args;
		args << QStringLiteral("put") << QStringLiteral("--kio-progress");
		if (flags & KIO::Overwrite) {
			args << QStringLiteral("--overwrite");
		}
		args << localPath << itemKioUrl(destItem);
		const KIO::filesize_t total = static_cast<KIO::filesize_t>(QFileInfo(localPath).size());
		return runCliProgress(args, total);
	}
	if (src.scheme() == QStringLiteral("fastonedrive") && dest.scheme() == QStringLiteral("fastonedrive")) {
		if (isProtectedPath(normalizedPath(dest))) {
			return KIO::WorkerResult::fail(KIO::ERR_CANNOT_WRITE, dest.toDisplayString());
		}
		const QString srcNorm = normalizedPath(src);
		const QString srcProfile = m_profileId;
		const QString srcItem = itemPath(srcNorm);
		const QString destNorm = normalizedPath(dest);
		const QString destProfile = m_profileId;
		const QString destItem = itemPath(destNorm);
		if (srcItem.isEmpty() || destItem.isEmpty()) {
			return KIO::WorkerResult::fail(
				KIO::ERR_UNSUPPORTED_ACTION,
				QStringLiteral("Só é possível copiar itens do índice.")
			);
		}
		const KIO::WorkerResult existsCheck = refuseIfRemoteExists(destItem, flags, dest);
		if (!existsCheck.success()) {
			return existsCheck;
		}
		if (srcProfile != destProfile) {
			m_profileId.clear();
		}
		QStringList args;
		args << QStringLiteral("copy") << QStringLiteral("--kio-progress");
		if (flags & KIO::Overwrite) {
			args << QStringLiteral("--overwrite");
		}
		args << itemKioUrlFor(srcProfile, srcItem) << itemKioUrlFor(destProfile, destItem);
		return runCliProgress(args, 0);
	}
	if (dest.scheme() != QStringLiteral("file")) {
		return KIO::WorkerResult::fail(
			KIO::ERR_UNSUPPORTED_ACTION,
			QStringLiteral("Cópia Fast-OneDrive só entre este Local e ficheiros locais.")
		);
	}
	const QString path = normalizedPath(src);
	const QString remotePath = itemPath(path);
	if (remotePath.isEmpty()) {
		return KIO::WorkerResult::fail(
			KIO::ERR_UNSUPPORTED_ACTION,
			QStringLiteral("Só é possível copiar itens do índice.")
		);
	}
	const QString destPath = dest.toLocalFile();
	if (destPath.isEmpty()) {
		return KIO::WorkerResult::fail(KIO::ERR_MALFORMED_URL, dest.toDisplayString());
	}
	QString errorText;
	const QJsonObject item = runJsonObject(
		QStringList() << QStringLiteral("item") << remotePath,
		&errorText
	);
	if (!errorText.isEmpty() || item.isEmpty()) {
		return failFromCli(errorText);
	}
	const QString localPath = item.value(QStringLiteral("localPath")).toString();
	if (!localPath.isEmpty()) {
		return copyLocalPath(localPath, destPath, flags);
	}
	const bool isDir = item.value(QStringLiteral("isDir")).toBool();
	const bool tooLarge = item.value(QStringLiteral("tooLargeToOpen")).toBool();
	const QFileInfo destInfo(destPath);
	if (destInfo.exists() && !(flags & KIO::Overwrite)) {
		if (destInfo.isDir()) {
			return KIO::WorkerResult::fail(KIO::ERR_DIR_ALREADY_EXIST, destPath);
		}
		return KIO::WorkerResult::fail(KIO::ERR_FILE_ALREADY_EXIST, destPath);
	}
	const qint64 size = item.value(QStringLiteral("size")).toVariant().toLongLong();
	const KIO::filesize_t total = size > 0 ? static_cast<KIO::filesize_t>(size) : 0;
	if (!isDir && !tooLarge) {
		QString plain;
		const KIO::WorkerResult hydrated = runCliProgress(
			QStringList()
				<< QStringLiteral("hydrate")
				<< QStringLiteral("--kio-progress")
				<< itemKioUrl(remotePath),
			total,
			QString(),
			&plain
		);
		if (!hydrated.success()) {
			return hydrated;
		}
		const QString cachePath = plain.trimmed().section(QLatin1Char('\n'), -1).trimmed();
		if (cachePath.isEmpty()) {
			return failFromCli(QStringLiteral("hydrate não devolveu caminho local"));
		}
		return copyLocalPath(cachePath, destPath, flags);
	}
	if (!isDir && tooLarge && isLauncherTemp(destPath)) {
		const QString fusePath = item.value(QStringLiteral("fusePath")).toString().isEmpty()
			? fuseItemPath(remotePath)
			: item.value(QStringLiteral("fusePath")).toString();
		const bool ready = item.contains(QStringLiteral("fuseReady"))
			? item.value(QStringLiteral("fuseReady")).toBool()
			: fuseMountReady();
		if (!ready) {
			return KIO::WorkerResult::fail(
				KIO::ERR_CANNOT_OPEN_FOR_READING,
				QStringLiteral("Activa o FUSE do Fast-OneDrive para abrir este ficheiro.")
			);
		}
		QFile::remove(destPath);
		if (!QFile::link(fusePath, destPath)) {
			return KIO::WorkerResult::fail(KIO::ERR_CANNOT_WRITE, destPath);
		}
		if (total > 0) {
			processedSize(total);
		}
		return KIO::WorkerResult::pass();
	}
	return runCliProgress(
		QStringList()
			<< QStringLiteral("copy")
			<< QStringLiteral("--kio-progress")
			<< itemKioUrl(remotePath)
			<< destPath,
		total,
		destPath
	);
}

KIO::WorkerResult FastOneDriveWorker::chmod(const QUrl &url, int permissions)
{
	const QString path = normalizedPath(url);
	if (isProtectedPath(path)) {
		return KIO::WorkerResult::pass();
	}
	const QString remote = itemPath(path);
	if (remote.isEmpty()) {
		return KIO::WorkerResult::pass();
	}
	const QString mode = QString::number(permissions & 0777, 8).rightJustified(4, QLatin1Char('0'));
	return runCli(
		QStringList()
			<< QStringLiteral("chmod")
			<< mode
			<< itemKioUrl(remote)
	);
}

bool FastOneDriveWorker::isPreviewRequest() const
{
	return hasMetaData(QStringLiteral("thumbnail")) || hasMetaData(QStringLiteral("preview"));
}

KIO::WorkerResult FastOneDriveWorker::redirectLocalFile(const QString &localPath)
{
	const QFileInfo info(localPath);
	if (!info.isFile()) {
		return KIO::WorkerResult::fail(KIO::ERR_CANNOT_OPEN_FOR_READING, localPath);
	}
	redirection(QUrl::fromLocalFile(info.absoluteFilePath()));
	return KIO::WorkerResult::pass();
}

KIO::WorkerResult FastOneDriveWorker::copyLocalPath(
	const QString &localPath,
	const QString &destPath,
	KIO::JobFlags flags
)
{
	const QFileInfo destInfo(destPath);
	if (destInfo.exists() && !(flags & KIO::Overwrite)) {
		if (destInfo.isDir()) {
			return KIO::WorkerResult::fail(KIO::ERR_DIR_ALREADY_EXIST, destPath);
		}
		return KIO::WorkerResult::fail(KIO::ERR_FILE_ALREADY_EXIST, destPath);
	}
	const QFileInfo localInfo(localPath);
	if (destInfo.exists() && localInfo.isDir() != destInfo.isDir()) {
		return KIO::WorkerResult::fail(KIO::ERR_CANNOT_OPEN_FOR_WRITING, destPath);
	}
	QDir().mkpath(destInfo.absolutePath());
	const QString staging = destInfo.absolutePath()
		+ QStringLiteral("/.fod-staging-")
		+ destInfo.fileName();
	if (QFileInfo(staging).isDir() && !QFileInfo(staging).isSymLink()) {
		QDir(staging).removeRecursively();
	} else {
		QFile::remove(staging);
	}
	if (localInfo.isDir()) {
		QString errorText;
		int exitCode = 1;
		runProcess(
			QStringLiteral("/usr/bin/cp"),
			QStringList() << QStringLiteral("-a") << localPath << staging,
			&errorText,
			&exitCode
		);
		if (exitCode != 0) {
			QDir(staging).removeRecursively();
			return failFromCli(errorText);
		}
	} else if (!QFile::copy(localPath, staging)) {
		QFile::remove(staging);
		return KIO::WorkerResult::fail(KIO::ERR_CANNOT_OPEN_FOR_WRITING, destPath);
	}
	if (!placeStagingOver(staging, destPath)) {
		if (QFileInfo(staging).isDir() && !QFileInfo(staging).isSymLink()) {
			QDir(staging).removeRecursively();
		} else {
			QFile::remove(staging);
		}
		return KIO::WorkerResult::fail(KIO::ERR_CANNOT_OPEN_FOR_WRITING, destPath);
	}
	if (!localInfo.isDir()) {
		processedSize(static_cast<KIO::filesize_t>(localInfo.size()));
	}
	return KIO::WorkerResult::pass();
}

KIO::WorkerResult FastOneDriveWorker::runCli(const QStringList &args)
{
	QString errorText;
	int exitCode = 1;
	runProcess(cliBin(), withProfile(args), &errorText, &exitCode);
	if (exitCode != 0) {
		return failFromCli(errorText);
	}
	return KIO::WorkerResult::pass();
}

void FastOneDriveWorker::flushProgress(
	QByteArray *buffer,
	KIO::filesize_t knownTotal,
	QString *plainOut
)
{
	while (true) {
		const int newline = buffer->indexOf('\n');
		if (newline < 0) {
			break;
		}
		QByteArray line = buffer->left(newline).trimmed();
		buffer->remove(0, newline + 1);
		if (!line.startsWith("FODPROGRESS ")) {
			if (plainOut != nullptr && !line.isEmpty()) {
				if (!plainOut->isEmpty()) {
					plainOut->append(QLatin1Char('\n'));
				}
				plainOut->append(QString::fromUtf8(line));
			}
			continue;
		}
		const QList<QByteArray> parts = line.split(' ');
		if (parts.size() < 3) {
			continue;
		}
		bool okBytes = false;
		const qulonglong bytes = parts.at(1).toULongLong(&okBytes);
		if (!okBytes) {
			continue;
		}
		if (knownTotal == 0) {
			bool okTotal = false;
			const qulonglong reported = parts.at(2).toULongLong(&okTotal);
			if (okTotal && reported > 0) {
				totalSize(static_cast<KIO::filesize_t>(reported));
			}
		}
		processedSize(static_cast<KIO::filesize_t>(bytes));
	}
}

KIO::filesize_t FastOneDriveWorker::partialBytes(const QString &destPath) const
{
	if (destPath.isEmpty()) {
		return 0;
	}
	const QFileInfo dest(destPath);
	const QDir dir(dest.absolutePath());
	const QString prefix = dest.fileName() + QStringLiteral(".fod-partial");
	KIO::filesize_t best = 0;
	const QFileInfoList infos = dir.entryInfoList(
		QStringList() << prefix << prefix + QLatin1String("*"),
		QDir::Files | QDir::Hidden
	);
	for (const QFileInfo &info : infos) {
		const KIO::filesize_t size = static_cast<KIO::filesize_t>(info.size());
		if (size > best) {
			best = size;
		}
	}
	if (dest.exists() && dest.isFile()) {
		const KIO::filesize_t size = static_cast<KIO::filesize_t>(dest.size());
		if (size > best) {
			best = size;
		}
	}
	return best;
}

KIO::WorkerResult FastOneDriveWorker::runCliProgress(
	const QStringList &args,
	KIO::filesize_t knownTotal,
	const QString &localDest,
	QString *plainOut
)
{
	if (knownTotal > 0) {
		totalSize(knownTotal);
		processedSize(0);
	}
	QProcess process;
	process.setProcessChannelMode(QProcess::SeparateChannels);
	QProcessEnvironment env = QProcessEnvironment::systemEnvironment();
	env.insert(QStringLiteral("PYTHONUNBUFFERED"), QStringLiteral("1"));
	process.setProcessEnvironment(env);
	process.start(cliBin(), withProfile(args));
	if (!process.waitForStarted(10000)) {
		return failFromCli(QStringLiteral("falha ao executar %1").arg(cliBin()));
	}
	QByteArray stdoutBuf;
	QString stderrText;
	while (process.state() != QProcess::NotRunning) {
		if (wasKilled()) {
			killProcessTree(process.processId());
			process.waitForFinished(1500);
			return KIO::WorkerResult::fail(KIO::ERR_USER_CANCELED, QStringLiteral("cópia cancelada"));
		}
		process.waitForReadyRead(200);
		stdoutBuf += process.readAllStandardOutput();
		stderrText += QString::fromUtf8(process.readAllStandardError());
		flushProgress(&stdoutBuf, knownTotal, plainOut);
		const KIO::filesize_t disk = partialBytes(localDest);
		if (disk > 0) {
			processedSize(disk);
		}
	}
	process.waitForFinished(-1);
	stdoutBuf += process.readAllStandardOutput();
	stderrText += QString::fromUtf8(process.readAllStandardError());
	flushProgress(&stdoutBuf, knownTotal, plainOut);
	if (plainOut != nullptr && !stdoutBuf.trimmed().isEmpty()) {
		if (!plainOut->isEmpty()) {
			plainOut->append(QLatin1Char('\n'));
		}
		plainOut->append(QString::fromUtf8(stdoutBuf.trimmed()));
	}
	if (wasKilled()) {
		return KIO::WorkerResult::fail(KIO::ERR_USER_CANCELED, QStringLiteral("cópia cancelada"));
	}
	if (process.exitCode() != 0) {
		return failFromCli(stderrText.trimmed());
	}
	if (knownTotal > 0) {
		processedSize(knownTotal);
	}
	return KIO::WorkerResult::pass();
}

KIO::WorkerResult FastOneDriveWorker::put(const QUrl &url, int permissions, KIO::JobFlags flags)
{
	Q_UNUSED(permissions);
	const QString path = normalizedPath(url);
	if (isProtectedPath(path)) {
		return KIO::WorkerResult::fail(KIO::ERR_CANNOT_WRITE, url.toDisplayString());
	}
	const QString remotePath = itemPath(path);
	if (remotePath.isEmpty()) {
		return KIO::WorkerResult::fail(KIO::ERR_CANNOT_WRITE, url.toDisplayString());
	}
	const KIO::WorkerResult existsCheck = refuseIfRemoteExists(remotePath, flags, url);
	if (!existsCheck.success()) {
		return existsCheck;
	}
	QTemporaryFile tmp;
	tmp.setAutoRemove(true);
	if (!tmp.open()) {
		return KIO::WorkerResult::fail(KIO::ERR_CANNOT_WRITE, url.toDisplayString());
	}
	QByteArray buffer;
	int bytes = 0;
	do {
		dataReq();
		bytes = readData(buffer);
		if (bytes < 0) {
			return KIO::WorkerResult::fail(KIO::ERR_CANNOT_READ, url.toDisplayString());
		}
		if (bytes > 0) {
			const qint64 written = tmp.write(buffer);
			if (written != static_cast<qint64>(bytes)) {
				return KIO::WorkerResult::fail(KIO::ERR_CANNOT_WRITE, url.toDisplayString());
			}
		}
	} while (bytes > 0);
	if (!tmp.flush()) {
		return KIO::WorkerResult::fail(KIO::ERR_CANNOT_WRITE, url.toDisplayString());
	}
	const KIO::filesize_t total = static_cast<KIO::filesize_t>(tmp.size());
	processedSize(total);
	QStringList args;
	args << QStringLiteral("put") << QStringLiteral("--kio-progress");
	if (flags & KIO::Overwrite) {
		args << QStringLiteral("--overwrite");
	}
	args << tmp.fileName() << itemKioUrl(remotePath);
	return runCliProgress(args, total);
}

KIO::WorkerResult FastOneDriveWorker::mkdir(const QUrl &url, int permissions)
{
	Q_UNUSED(permissions);
	const QString path = normalizedPath(url);
	if (isProtectedPath(path)) {
		return KIO::WorkerResult::fail(KIO::ERR_CANNOT_MKDIR, url.toDisplayString());
	}
	const QString remotePath = itemPath(path);
	if (remotePath.isEmpty()) {
		return KIO::WorkerResult::fail(KIO::ERR_CANNOT_MKDIR, url.toDisplayString());
	}
	return runCli(
		QStringList()
			<< QStringLiteral("mkdir")
			<< QStringLiteral("--notify")
			<< itemKioUrl(remotePath)
	);
}

KIO::WorkerResult FastOneDriveWorker::del(const QUrl &url, bool isfile)
{
	const QString path = normalizedPath(url);
	if (isProtectedPath(path)) {
		return KIO::WorkerResult::fail(KIO::ERR_CANNOT_DELETE, url.toDisplayString());
	}
	const QString remotePath = itemPath(path);
	if (remotePath.isEmpty()) {
		return KIO::WorkerResult::fail(KIO::ERR_CANNOT_DELETE, url.toDisplayString());
	}
	QStringList args;
	args << QStringLiteral("rm") << QStringLiteral("--notify");
	if (isfile) {
		args << QStringLiteral("--file");
	} else {
		args << QStringLiteral("--dir");
	}
	args << itemKioUrl(remotePath);
	return runCli(args);
}

KIO::WorkerResult FastOneDriveWorker::rename(const QUrl &src, const QUrl &dest, KIO::JobFlags flags)
{
	if (dest.scheme() != QStringLiteral("fastonedrive")) {
		return KIO::WorkerResult::fail(
			KIO::ERR_UNSUPPORTED_ACTION,
			QStringLiteral("Renomear só dentro do Local OneDrive.")
		);
	}
	const QString srcPath = normalizedPath(src);
	const QString srcProfile = m_profileId;
	const QString destPath = normalizedPath(dest);
	const QString destProfile = m_profileId;
	if (isProtectedPath(srcPath) || isProtectedPath(destPath)) {
		return KIO::WorkerResult::fail(KIO::ERR_CANNOT_RENAME, src.toDisplayString());
	}
	const QString srcItem = itemPath(srcPath);
	const QString destItem = itemPath(destPath);
	if (srcItem.isEmpty() || destItem.isEmpty()) {
		return KIO::WorkerResult::fail(KIO::ERR_CANNOT_RENAME, src.toDisplayString());
	}
	const KIO::WorkerResult existsCheck = refuseIfRemoteExists(destItem, flags, dest);
	if (!existsCheck.success()) {
		return existsCheck;
	}
	if (srcProfile != destProfile) {
		m_profileId.clear();
	}
	QStringList args;
	args << QStringLiteral("mv") << QStringLiteral("--notify");
	if (flags & KIO::Overwrite) {
		args << QStringLiteral("--overwrite");
	}
	args << itemKioUrlFor(srcProfile, srcItem) << itemKioUrlFor(destProfile, destItem);
	return runCli(args);
}

KIO::WorkerResult FastOneDriveWorker::refuseIfRemoteExists(
	const QString &remotePath,
	KIO::JobFlags flags,
	const QUrl &url
)
{
	if (flags & KIO::Overwrite) {
		return KIO::WorkerResult::pass();
	}
	QString errorText;
	const QJsonObject existing = runJsonObject(
		QStringList() << QStringLiteral("exists") << itemKioUrl(remotePath),
		&errorText
	);
	if (!errorText.isEmpty()) {
		return failFromCli(errorText);
	}
	if (existing.value(QStringLiteral("exists")).toBool()) {
		if (existing.value(QStringLiteral("isDir")).toBool()) {
			return KIO::WorkerResult::fail(KIO::ERR_DIR_ALREADY_EXIST, url.toDisplayString());
		}
		return KIO::WorkerResult::fail(KIO::ERR_FILE_ALREADY_EXIST, url.toDisplayString());
	}
	return KIO::WorkerResult::pass();
}

class KIOPluginForMetaData : public QObject
{
	Q_OBJECT
	Q_PLUGIN_METADATA(IID "org.kde.kio.worker.fastonedrive" FILE "kio_fastonedrive.json")
};

extern "C" int Q_DECL_EXPORT kdemain(int argc, char **argv)
{
	QCoreApplication app(argc, argv);
	if (argc != 4) {
		return -1;
	}
	FastOneDriveWorker worker(argv[2], argv[3]);
	worker.dispatchLoop();
	return 0;
}

#include "kio_fastonedrive.moc"
