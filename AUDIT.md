# Auditoria por fluxo

«Limpo» vale só para o fluxo cuja lista de funções foi preenchida depois da última edição desses ficheiros. Não descreve o projeto inteiro.

`index.py`, `search.py`, `syncpins.py`, `mutate.py`, `copy.py` e `cli.py` foram alterados depois da leitura anterior. Esses fluxos voltaram a `por reler`. Nesta passagem foram corrigidos e relidos `_migrateBaselineTokens`, `_intactLocalFile`, `removeRemotePath`, `_moveAcrossProfiles` e `unlink`. O resto desses fluxos continua por reler. O registo não descreve o projeto inteiro.

| Fluxo | Ficheiros | Estado | Achados em aberto |
|---|---|---|---|
| listDir, refresh, delta, rebuild | `index.py`, `graph.py`, `search.py` | por reler | 0 |
| pin, skipPin, hydrate, sync-pins | `syncpins.py`, `pin.py`, `hydrate.py`, `chunks.py` | por reler | 0 |
| FUSE e watcher | `fuse.py`, `watcher.py`, `disk.py` | por reler | 0 |
| Delete, cópia grande, worker KIO | `mutate.py`, `copy.py`, `cli.py`, `kio/kio_fastonedrive.cpp`, `search.py` (`rowToDict`, `chmodItem`) | por reler | 0 |
| URL, perfis, notify, mount | `paths.py`, `profiles.py`, `cli.py`, `notify.py`, `mount.py`, `config.py`, `kioinstall.py` (Locais) | por reler | 0 |
| Login de perfil | `oauthsetup.py`, `wizard.py` | limpo nesta leitura | 0 |
| Definições | `settings.py`, `config.py` (`importSystemRcloneConfig`), `rclone.py` (`remoteAbout`) | limpo nesta leitura | 0 |
| KRunner | `krunner.py`, `menus.py` (`writeInstalledText`) | limpo nesta leitura | 0 |
| Menus Dolphin | `menus.py`, `dolphin/servicemenu.py` | limpo nesta leitura | 0 |
| Instalação do worker | `kioinstall.py`, `kio/Makefile`, `menus.py` (`writeInstalledText`) | limpo nesta leitura | 0 |
| Busca | `search.py`, `cli.py`, `kio/kio_fastonedrive.cpp` (`listDir` da busca), `kioinstall.py` (`runDolphinSearch`) | por reler | 0 |
| Partilhar | `rclone.py` (`linkRemote`), `cli.py` (`_cmdLink`) | por reler | 0 |
| Actividade da bandeja | `activity.py`, `syncpins.py` (`setActivity`), `settings.py` (`refreshTip`) | por reler | 0 |

`kioinstall.py` entra no fluxo dos Locais pelas funções do `user-places.xbel` e no fluxo da instalação pelas funções do worker. `_endDashPositionals` corre antes do `parse` e não altera o argv de `put`, `mkdir`, `rm`, `mv`, `copy` nem `hydrate`.

## Achados

| Gravidade | Fluxo | Onde | Como ocorria | Estado |
|---|---|---|---|---|
| Crítico | pin, skipPin, hydrate, sync-pins | `syncpins.py` `_keepPinnedChild` | Unpin dentro de uma pasta pinada voltava a `pinned=1` com o `baseEtag` antigo e o sync apagava a nuvem. Agora, se não há cópia local, `clearItemBaseline` faz o ciclo descarregar. | corrigido, fluxo por reler |
| Crítico | pin, skipPin, hydrate, sync-pins | `chunks.py` `noteWrite`, `_fillSpan` | Intervalos escritos só em memória. O outro processo preenchia o buraco com o remoto. Os intervalos passam a `chunk_writes`. | corrigido, fluxo por reler |
| Alto | listDir, refresh, delta, rebuild | `graph.py` `_parentPathHint`; `index.py` `_resolveDeltaPath` | Filho da raiz (`/drive/root:`) não resolvia. `parentAtRoot` devolve o nome. | corrigido, fluxo por reler |
| Alto | listDir, refresh, delta, rebuild | `index.py` `_flushDeltaDeletes` | O tombstone usava o caminho antigo em memória e apagava quem ocupou esse caminho. O caminho sai da linha actual do `itemId`. Sem linha, não há vanish. | corrigido, fluxo por reler |
| Alto | listDir, refresh, delta, rebuild | `index.py` `_rebuildGraph` | O 404 da pasta partilhada saía fora do `try`. O `try` inclui `_applyDeltaFeed`. | corrigido, fluxo por reler |
| Alto | listDir, refresh, delta, rebuild | `index.py` `_flushDeltaDeletes` | Apagar o atalho não tirava o scope e o outro delta reinsería a árvore. O scope sai antes do ciclo seguinte. | corrigido, fluxo por reler |
| Alto | pin, skipPin, hydrate, sync-pins | `hydrate.py` `_hydrateRemote` | Cache mais novo que a baseline, sem `syncState`, era substituído. `isCacheDirty` também bloqueia a substituição. | corrigido, fluxo por reler |
| Alto | pin, skipPin, hydrate, sync-pins | `syncpins.py` `_promoteDirtyCache` | O guard era só testado. A cópia corre com `tryExclusiveWriteGuard`. | corrigido, fluxo por reler |
| Médio | pin, skipPin, hydrate, sync-pins | `hydrate.py` `isCacheDirty` | `error` não contava como dirty. Entra na mesma lista que `dirty`. | corrigido, fluxo por reler |
| Médio | pin, skipPin, hydrate, sync-pins | `syncpins.py` `_resolveHashedBaseline` | QuickXor inconclusivo com mtime próximo adoptava a baseline. Fica `error` e não adopta. | corrigido, fluxo por reler |
| Alto | FUSE e watcher | `fuse.py` `open` | Leitura até `openMaxBytes` devolvia `EACCES`. Leitura usa sparse. Escrita até ao limiar continua a hidratar. Miniatura segue bloqueada. | corrigido, fluxo por reler |
| Alto | FUSE e watcher | `watcher.py` `_scheduleMovedTree`; `syncpins.py` `_syncOnePath` | Mover uma pasta sem pin descarregava a árvore para `pinned/`. Sem `pinned=1` e sem pasta em `pinned/`, o sync da pasta não corre. | corrigido, fluxo por reler |
| Alto | FUSE e watcher | `watcher.py` `_readEvents` | O par `.goutputstream-*` / nome final não sincronizava. Só `.fod-staging-*`, `.fod-partial` e `.fod-bak-*` ficam fora da fila. | corrigido, fluxo por reler |
| Alto | FUSE e watcher | `fuse.py` `_openForWrite` | Abrir para escrita marcava dirty antes de escrever. Dirty segue `write`, `truncate` ou `O_TRUNC`. | corrigido, fluxo por reler |
| Médio | FUSE e watcher | `watcher.py` `_watchLoop` | Uma excepção fechava o inotify. O ciclo regista e continua. | corrigido, fluxo por reler |
| Alto | Delete, cópia grande, worker KIO | `kio_fastonedrive.cpp` `copy`, `rename`; `cli.py` `_prepareProfile`; `mutate.py` `_moveAcrossProfiles` | Os dois URL saíam com o perfil do destino. Cada URL leva o seu perfil. `copy` e `mv` sem `--profile` aceitam perfis diferentes. O move entre perfis resolve cada remote no seu perfil. | corrigido, fluxo por reler |
| Alto | Delete, cópia grande, worker KIO | `mutate.py` `_moveAcrossProfiles` | A correcção acima apagava o índice e as cópias no perfil do destino. A limpeza da origem corre com o perfil da origem; o índice do destino é o do destino. | corrigido, fluxo por reler |
| Alto | Delete, cópia grande, worker KIO | `mutate.py` `_pendingLocalEdit` | Delete ignorava pin com mtime mais novo que a baseline, e pin sem linha no índice. Esses casos recusam o Delete. | corrigido, fluxo por reler |
| Médio | Delete, cópia grande, worker KIO | `mutate.py` `removeRemotePath` | Stat vazio apagava `pinned/` e `cache/`. Com cópia local, o Delete falha e a cópia fica. Sem cópia local, sai só a linha do índice. | corrigido, fluxo por reler |
| Médio | Delete, cópia grande, worker KIO | `kio_fastonedrive.cpp` `put` | `write` ou `flush` falhado enviava o temporário. O `put` falha e não chama o CLI. | corrigido, fluxo por reler |
| Médio | listDir, refresh, delta, rebuild | `index.py` `_rebuildGraph` | Primeiro ciclo sem `deltaLink` gravava o vazio e corria `_dropUnseenRows`. Link vazio levanta `GraphDeltaExpired`. | corrigido, fluxo por reler |
| Médio | listDir, refresh, delta, rebuild | `index.py` `_removeVanishedCopies` | `rmdir` falhado apagava a linha na mesma. Se a cópia local não sai, a linha fica. | corrigido, fluxo por reler |
| Alto | listDir, refresh, delta, rebuild | `graph.py:93` `_parentPathHint`; `index.py:1991` `_resolveDeltaPath` | `parentReference.path` vinha percent-encoded e ganhava ao `itemId` do pai. Filho de `Meu Trabalho` gravava `Meu%20Trabalho/…`. O hint é descodificado e só serve se o pai ainda não está no mapa. | corrigido, fluxo por reler |
| Alto | listDir, refresh, delta, rebuild | `index.py:1710` `_rememberSharedScope`; `index.py:1433` `_rebuildGraph` | O scope da pasta partilhada só era gravado depois de um delta limpo, e o cursor da raiz já tinha avançado. Falha, órfão ou crash perdia a subscrição. O scope grava-se quando o atalho resolve, antes do cursor da raiz. | corrigido, fluxo por reler |
| Alto | listDir, refresh, delta, rebuild | `index.py:954` `_retargetChunks` | O move deixava `chunk_writes` do destino e `renameIndexPath` chocava com a chave de `file_chunks`. O destino sai antes de o intervalo mudar de caminho. | corrigido, fluxo por reler |
| Médio | listDir, refresh, delta, rebuild | `index.py:1599` `_applyDeltaFeed` | Todo o delete corria depois de todo o update. A última ocorrência viva no mesmo feed perdia. A ocorrência viva tira o delete da lista. | corrigido, fluxo por reler |
| Médio | listDir, refresh, delta, rebuild | `index.py:1957` `_removeVanishedCopies` | Ficheiro no lugar de uma pasta, ou o inverso, não saía e a linha saía na mesma. Se o caminho local existe e não sai, a linha fica. | corrigido, fluxo por reler |
| Alto | listDir, refresh, delta, rebuild | `index.py` `_flushDeltaPending`, `_releaseVanishedTree` | Item movido para um caminho excluído do índice ficava no caminho antigo quando `baseEtag` estava vazio. Esse move passa `keepUnsynced=False`: a árvore antiga sai também com baseline vazia. Dirty, syncing e conflict ficam. O delete na nuvem e a listagem continuam a manter baseline vazia. | corrigido, fluxo por reler |
| Baixo | listDir, refresh, delta, rebuild | `index.py:316` `forgetFileDescendants` | Ao apagar o fantasma saíam `files` e `file_chunks`, e `chunk_writes` ficava. Sai também. | corrigido, fluxo por reler |
| Alto | listDir, refresh, delta, rebuild | `index.py:1831` `_flushDeltaPending` | O lote gravava o caminho resolvido antes do move de um antepassado. O `UPSERT` recriava o caminho antigo e chocava com `idx_files_itemId`. O lote usa o `ids` depois dos retargets. | corrigido, fluxo por reler |
| Alto | listDir, refresh, delta, rebuild | `index.py:2046` `_resolveDeltaPath` | O hint entrava na primeira volta, antes do item do pai no mesmo lote. Sem hint, o filho espera; o hint só entra se o pai não está no mapa nem no lote. Filho da raiz continua em `parentAtRoot`. | corrigido, fluxo por reler |
| Alto | listDir, refresh, delta, rebuild | `index.py:2105` `_displaceOccupiedTree` | Mover para um caminho ocupado apagava a cópia local do ocupante e deixava a linha, ou substituía o `itemId` antes do tombstone. A árvore ocupante vai para um caminho de conflito, com `itemId`, pin e baseline. O tombstone do mesmo feed ainda a encontra. | corrigido, fluxo por reler |
| Alto | listDir, refresh, delta, rebuild | `index.py:1052` `renameIndexPath`; `index.py:2297` `_moveIndexPaths` | O destino ficava com `file_chunks` e `chunk_writes` quando a origem não tinha linha nesse caminho. Esses intervalos saem antes de a origem mudar de caminho. | corrigido, fluxo por reler |
| Alto | pin, skipPin, hydrate, sync-pins | `pin.py:245` `storedPinFlag` | `isPinnedHereOrAbove` não estava importado. Criar, renomear ou indexar um caminho fora do `skipPin` levantava `NameError` e a flag de pin não era gravada. A função passa a ser importada de `index.py`. | corrigido, fluxo por reler |
| Crítico | pin, skipPin, hydrate, sync-pins | `syncpins.py:848` `_uploadLocal` | O exclusivo do ficheiro era pedido outra vez dentro do mesmo exclusivo. O segundo pedido falhava e o envio de um pin, ou de uma cópia de conflito, voltava sem subir. A promoção do cache corre antes desse exclusivo. | corrigido, fluxo por reler |
| Crítico | pin, skipPin, hydrate, sync-pins | `syncpins.py` `_ensurePinnedCopy`; `hydrate.py` `adoptDownloadedFile` | Um pin já marcado voltava cedo e mantinha a baseline sem cópia em `pinned/`. A eviction do `cache/` fazia o ciclo apagar a nuvem. Sem `pinned/` e sem cache, a baseline sai e o ciclo descarrega. Cache íntegro é copiado para `pinned/` antes. Hidratar só para `cache/` não grava baseline. | corrigido, fluxo por reler |
| Médio | FUSE e watcher | `fuse.py:115` `readdir` | O índice omitia `.goutputstream-*`, `*.tmp` e `*.swp`, e a passagem pelo disco voltava a metê-los na lista. Esses nomes ficam de fora. | corrigido, fluxo por reler |
| Médio | FUSE e watcher | `fuse.py:284` `truncate` | `truncate` sem handle marcava `dirty` e o inotify não vê `IN_MODIFY`. O envio não era agendado. No fim, se já não há buracos, corre `scheduleItemSync`. | corrigido, fluxo por reler |
| Médio | FUSE e watcher | `fuse.py:226` `create` | `upsertEntry` seguido de `open` falhado deixava a linha. Sem ficheiro e sem filhos, a linha sai. | corrigido, fluxo por reler |
| Médio | FUSE e watcher | `disk.py:349` `_evictionBlocked` | `open` para escrita falhava num cache `0444` e a eviction tratava o ficheiro como ocupado. O teste do `flock` abre para leitura. | corrigido, fluxo por reler |
| Crítico | Delete, cópia grande, worker KIO | `copy.py` `copyDriveToDrive`; `mutate.py` `putLocalToRemote` | Substituir um destino com alterações locais apagava `pinned/` e `cache/` e, no `put`, limpava `dirty`. Essas cópias saem para um nome de conflito antes da escrita na nuvem. O `itemId` só é limpo depois de a escrita concluir. Se a escrita falha, a cópia volta ao caminho. | corrigido, fluxo por reler |
| Alto | Delete, cópia grande, worker KIO | `mutate.py` `_refuseDestination` | Sem `--overwrite`, um destino que já não estava na nuvem mas ainda tinha cópia local era substituído. A operação passa a recusar. | corrigido, fluxo por reler |
| Alto | Delete, cópia grande, worker KIO | `kio_fastonedrive.cpp` `copyLocalPath` | A cópia local apagava o destino e só depois escrevia. Se a escrita falhava, o destino já não existia. A escrita vai para `.fod-staging-` e só então troca com o destino. | corrigido, fluxo por reler |
| Alto | URL, perfis, notify, mount | `paths.py:129` `_normalizeKioPath` | `fastonedrive:/item/…` mantinha o perfil já activo. A cópia de `/p/<outro>/` para o URL antigo escrevia na conta errada. Sem id no URL, activa o perfil migrado. | corrigido, fluxo por reler |
| Alto | URL, perfis, notify, mount | `paths.py:69` `isRemotePath`; `paths.py:98` `_normalizeRemotePath` | `trabalho:pasta` com outro perfil activo era caminho local. A cópia entre remotes falhava. O remote de cada perfil activa esse perfil. | corrigido, fluxo por reler |
| Médio | URL, perfis, notify, mount | `profiles.py:132` `removeProfile` | O token saía antes de apagar os dados. Se o `rmtree` falhava, a conta ficava sem token e com ficheiros por enviar. O token sai depois do `rmtree`. | corrigido, fluxo por reler |
| Médio | URL, perfis, notify, mount | `profiles.py:34` `migrateLegacyIfNeeded` | Dois processos migravam ao mesmo tempo e um `shutil.move` falhava a meio. A migração corre sob `rcloneConfigLock` e o segundo vê o perfil já gravado. | corrigido, fluxo por reler |
| Médio | URL, perfis, notify, mount | `mount.py:65` `setMountEnabled` | `enable --now` falhado deixava `mountEnabled` verdadeiro. Se a unidade não fica activa, a flag volta ao valor anterior. | corrigido, fluxo por reler |
| Médio | URL, perfis, notify, mount | `config.py:709` `_writeUserConfig` | `write_text` truncava o `config.toml`. Um crash deixava o ficheiro pela metade. A escrita vai para um temporário e só depois substitui. | corrigido, fluxo por reler |
| Médio | URL, perfis, notify, mount | `kioinstall.py:338` `_rewriteFusePlaces` | O primeiro mount que era prefixo do caminho ganhava. Um mount dentro de outro ia para o perfil de cima. Ganha o mount com mais componentes. | corrigido, fluxo por reler |
| Médio | URL, perfis, notify, mount | `kioinstall.py:556` `_writePlaces` | `write_text` truncava o `user-places.xbel`. A escrita vai para um temporário e só depois substitui. | corrigido, fluxo por reler |
| Baixo | URL, perfis, notify, mount | `notify.py:120` `_emitKdirNotify` | No `dbus-send`, uma vírgula no nome partia o array. A vírgula segue como `%2C`. | corrigido, fluxo por reler |
| Alto | Login de perfil | `oauthsetup.py:50` `cancel`; `wizard.py:114` `reject` | Cancelar não matava o `rclone authorize`. O token podia gravar-se e o perfil nascer depois de a janela fechar. O processo fica registado e o fecho não cria o perfil. | corrigido, fluxo por reler |
| Alto | Login de perfil | `wizard.py:264` `_createProfile`; `oauthsetup.py:75` `_prepareRemote` | A falha depois do token deixava a secção completa sem perfil. A tentativa seguinte dizia que a conta já existia. A falha desfaz o perfil e a secção; uma secção órfã, sem perfil, volta a entrar. | corrigido, fluxo por reler |
| Alto | Definições | `settings.py:265` `_onProfileChanged` | A pesquisa ficava do perfil anterior. Abrir, localizar ou copiar usava a conta do perfil novo com o caminho da pesquisa antiga. A lista sai ao mudar, criar ou remover perfil. | corrigido, fluxo por reler |
| Alto | Definições | `settings.py:278` `_addProfile` | O assistente activava o perfil novo e, se o Local ou o mount falhava, limpava o perfil activo. A janela continuava a mostrar o perfil anterior e a pesquisa ia à raiz antiga. O perfil seleccionado é reposto. | corrigido, fluxo por reler |
| Alto | Definições | `settings.py:928` `_refreshQuota`; `rclone.py:464` `remoteAbout` | A quota em curso era mostrada no perfil seguinte e o pedido novo não arrancava. O pedido fixa o remote e o id; se o perfil mudou, o resultado é ignorado e a quota é pedida outra vez. | corrigido, fluxo por reler |
| Alto | Definições | `settings.py:1247` `_openSelected` | Hidratar mudava o perfil activo e a barra continuava no outro. O Guardar seguinte aplicava o formulário à conta errada. Durante o download a barra fica bloqueada; no fim o perfil de origem volta. | corrigido, fluxo por reler |
| Médio | Definições | `settings.py:1072` `_persist` | Guardar com `enable --now` falhado deixava `mountEnabled` verdadeiro. Testar gravava a flag sem chegar a montar. Se a unidade não fica activa, a flag e a caixa voltam ao valor anterior. Testar não muda a flag. | corrigido, fluxo por reler |
| Médio | Definições | `settings.py:302` `_renameProfile` | O nome entrava na config antes do título do Local. Confirmar o mesmo nome não repetia o título. O título é gravado primeiro. | corrigido, fluxo por reler |
| Médio | Definições | `settings.py:1082` `_persist`; `config.py:172` `storedRemoteName` | Guardar com outro perfil activo substituía o `remoteName` global. `status`, sem perfil activo, passava a mostrar essa conta. Com perfil activo a chave global fica e o combo volta ao remote do perfil. | corrigido, fluxo por reler |
| Médio | Definições | `config.py:360` `importSystemRcloneConfig` | `copy2` truncava o `rclone.conf` e não esperava pelo flock do token. A cópia vai para um temporário, com `fsync`, e só depois substitui. | corrigido, fluxo por reler |
| Médio | Definições | `settings.py:204` `closeEvent` | Sem bandeja, Fechar deixava o processo sem janela e sem Sair. Sem bandeja, Fechar termina o processo. | corrigido, fluxo por reler |
| Médio | Definições | `settings.py:1021` `_refreshFastStatus` | Índice, pins e cache mostravam a pasta do primeiro perfil. Os caminhos são refeitos ao carregar o perfil. | corrigido, fluxo por reler |
| Baixo | Definições | `settings.py:1392` `dropClient` | `settings --tray` com a janela já aberta deixava o socket na lista. O fecho lê o texto e solta o cliente uma vez. | corrigido, fluxo por reler |
| Baixo | KRunner | `krunner.py:144` `installRunner`; `menus.py:36` `writeInstalledText` | `write_text` truncava o `.desktop` e o serviço D-Bus. A escrita vai para um temporário e só depois substitui. | corrigido, fluxo por reler |
| Baixo | Menus Dolphin | `menus.py:181` `installMenus`; `menus.py:36` `writeInstalledText` | `write_text` e `copy2` truncavam o menu, o wrapper e os `.desktop`. A escrita vai para um temporário e só depois substitui. | corrigido, fluxo por reler |
| Alto | Instalação do worker | `kio/Makefile` `install` | `cp -f` truncava o `.so` que o Dolphin tinha mapeado. Se a cópia parava a meio, o plugin ficava incompleto. A cópia vai para `.kio_fastonedrive.so.tmp` e só depois troca o nome. | corrigido, fluxo por reler |
| Médio | Instalação do worker | `kioinstall.py:136` `_installPluginEnv`; `kioinstall.py:321` `_hideMountInHome`; `kioinstall.py:462` `_insertPlaceBookmark` | `write_text` truncava o script de sessão, o `environment.d`, o `.hidden` e, sem perfis, o `user-places.xbel`. A escrita vai para um temporário. O `environment.d` põe o directório do plugin à frente dos `QT_PLUGIN_PATH` dos `.conf` anteriores. | corrigido, fluxo por reler |
| Baixo | Instalação do worker | `kioinstall.py:155` `_buildAndInstallWorker` | Com `Makefile` e sem `make`, a instalação falhava mesmo havendo `cmake`. Sem `make`, usa cmake. O `make` chamado é o que está no `PATH`. | corrigido, fluxo por reler |
| Médio | Busca | `cli.py:366` `_endDashPositionals` | Um termo ou um caminho `item`/`children` a começar por `-` era lido como opção e o comando falhava. Esse argumento passa a ir depois de `--`. | corrigido, fluxo por reler |
| Baixo | Partilhar | `rclone.py:483` `linkRemote` | stdout só com espaços fazia `splitlines()[-1]` e levantava `IndexError`. Linhas vazias são ignoradas; sem URL, o erro é o de ligação vazia. | corrigido, fluxo por reler |
| Médio | Actividade da bandeja | `activity.py:9` `setActivity` | `write_text` truncava `activity.json`. A bandeja, noutro processo, lia o ficheiro a meio e mostrava inactivo durante o sync. A escrita troca o ficheiro inteiro. | corrigido, fluxo por reler |
| Alto | listDir, refresh, delta, rebuild | `index.py:2277` `_keepVanished` | Um envio falhado ficava `error` com a cópia local. O delta, ou o ciclo completo, tratava esse estado como limpo e apagava o ficheiro. `error` fica junto de `dirty`, `syncing` e `conflict`. Se a linha fica, o delete marca `conflict`. | corrigido, fluxo por reler |
| Médio | listDir, refresh, delta, rebuild | `index.py:523` `_listingOwnerAlive` | A fila de refresh guardava só o PID. Depois de o worker morrer, um PID reutilizado contava como dono e a pasta deixava de ser actualizada. O arranque do processo (`starttime`) acompanha o PID. | corrigido, fluxo por reler |
| Alto | Delete, cópia grande, worker KIO | `search.py:160` `rowToDict` | Com pin e cache sujo, `localPath` era o pin. `get` e `copy` entregavam os bytes antigos e deixavam as alterações por enviar de fora. O cache completo e sujo passa à frente do pin. | corrigido, fluxo por reler |
| Médio | Delete, cópia grande, worker KIO | `mutate.py:636` `_mirrorPinnedLocal` | O `put` de uma pasta, com o pai pinado, só criava o directório em `pinned/`. Os ficheiros ficavam só na nuvem. A árvore elegível é copiada e a baseline segue o stat remoto. | corrigido, fluxo por reler |
| Médio | Delete, cópia grande, worker KIO | `mutate.py:610` `_clearPathDirty` | Depois de um `put` que substituía o ficheiro, `error` continuava. O ciclo seguinte voltava a enviar. `error` sai junto de `dirty`. | corrigido, fluxo por reler |
| Médio | Delete, cópia grande, worker KIO | `search.py:112` `chmodItem` | Com pin e cache, o modo só era aplicado ao pin. A promoção do cache repunha o modo antigo. Os dois recebem `os.chmod`. | corrigido, fluxo por reler |
| Médio | Delete, cópia grande, worker KIO | `mutate.py:642` `_mirrorPinnedLocal` | `put` da própria cópia pinada fazia `copy2` do ficheiro para ele mesmo e falhava depois de a nuvem aceitar a escrita. O mesmo ficheiro não é copiado outra vez. | corrigido, fluxo por reler |
| Médio | URL, perfis, notify, mount | `mount.py:179` `_writeProfileUnit` | `write_text` truncava a unidade systemd. Um crash deixava o FUSE do perfil sem unidade válida. A escrita vai para um temporário e só depois substitui. | corrigido, fluxo por reler |
| Alto | URL, perfis, notify, mount | `profiles.py:316` `_rowHasPending` | Remover o perfil só via `syncState`. Uma cópia local mais nova que a baseline, ainda sem esse estado, era apagada com o perfil. Essa cópia passa a recusar a remoção. | corrigido, fluxo por reler |
| Alto | listDir, refresh, delta, rebuild | `index.py:1631` `_migrateBaselineTokens` | Cada `index` chamava a migração outra vez e gravava `migrated:` por cima de `mt:`. Um pin apagado no disco deixava de ser equivalente ao remoto e a nuvem não era apagada. `mt:`, `qxh:` e `sha1:` ficam. | corrigido, fluxo por reler |
| Alto | Delete, cópia grande, worker KIO | `copy.py:83` `_intactLocalFile` | Cópia dentro do Drive usava o pin, ou desistia do pin se o `cache/` tinha um sparse. O destino ficava sem a edição local. Cache sujo mais recente ganha ao pin; um sparse não esconde o pin completo. | corrigido, fluxo por reler |
| Alto | Delete, cópia grande, worker KIO | `mutate.py:400` `removeRemotePath`; `mutate.py:501` `_moveAcrossProfiles`; `fuse.py:409` `unlink` | A linha do índice saía antes da cópia local. Se o ficheiro ficava no disco, o stat seguinte voltava a enviá-lo. A cópia sai primeiro; se ainda está no disco, a linha fica. | corrigido, fluxo por reler |

## listDir, refresh, delta, rebuild

Funções que mudam índice, disco ou remoto, relidas depois da correcção: `rebuildIndex`, `refreshParentListing`, `forgetFileDescendants`, `scheduleParentRefresh`, `_refreshParentWorker`, `upsertLiveChildren`, `upsertEntry`, `setBaseline`, `clearItemBaseline`, `deleteIndexTree`, `renameIndexPath`, `_retargetChunks`, `_deleteForeignChunks`, `_rebuild`, `_rebuildGraph`, `_rebuildRclone`, `_applyDeltaFeed`, `_flushDeltaPending`, `_rememberSharedScope`, `_retargetIds`, `_retargetSharedPrefixes`, `_forgetReleasedIds`, `_flushDeltaDeletes`, `_releaseVanishedTree`, `_dropVanishedPath`, `_removeVanishedCopies`, `_resolveDeltaPath`, `_displaceOccupiedTree`, `_rewriteDescendantPaths`, `_moveIndexPaths`, `relocateStoredFiles`, `_dropUnseenRows`, `_commitIndexBatch`, `_saveDeltaState`, `listEntries`, `_statRemoteIntoIndex`.

`parseDeltaItem` e `_parentPathHint` não gravam estado. `_parentPathHint` descodifica o resto de `/root:` (`graph.py:93`). `_resolveDeltaPath` usa o pai já indexado (`index.py:1991`) e o hint só como reserva (`index.py:1999`).

### `rebuildIndex` (`index.py:172`)

1. Especificação: índice Graph incremental, 410 = ciclo completo (`docs/DECISIONS.md` decisão 21). `_rebuild` faz isso (`index.py:1365`).
2. Vazio: sem lock devolve -1 (`index.py:177`). Remote raiz vem de `config.remoteRoot` (`index.py:173`).
3. Identidade: o ciclo delega em `_rebuildGraph` / `_rebuildRclone`.
4. Outro processo: `index.lock` (`index.py:175`). O estado do job fica em ficheiro (`index.py:179`).
5. Falha a meio: o `except` grava o erro e relança (`index.py:202`). Lotes já com `commit` ficam.
6. Ciclo seguinte: cursor vazio ou `GraphDeltaExpired` refaz o ciclo completo (`index.py:1370`).
7. Quem chama: CLI `index` e `startBackgroundIndex` (`index.py:233`).

### `refreshParentListing` (`index.py:266`)

1. Especificação: `lsjson --max-depth 1` sem delta (`AGENTS.md`). `iterLsjson` sem `useOnedriveDelta` (`index.py:271`).
2. Vazio: caminho vazio não entra em `forgetFileDescendants` (`index.py:267`). Stat ausente trata como pasta (`index.py:330`).
3. Identidade: `upsertLiveChildren` move pelo `itemId` (`index.py:521`).
4. Outro processo: o worker usa `_listingGate` (`index.py:447`).
5. Falha a meio: a excepção sobe ao worker, que regista e não mata o processo (`index.py:456`).
6. Ciclo seguinte: o TTL está no ficheiro partilhado (`index.py:383`).
7. Quem chama: `_refreshParentWorker` (`index.py:448`) e `listEntries` (`search.py:279`).

### `forgetFileDescendants` (`index.py:282`)

1. Especificação: fantasma `nome/nome` deixado pelo `listDir` antigo. Apaga a linha e os intervalos.
2. Vazio: caminho vazio ou nome vazio volta (`index.py:283`).
3. Identidade: só apaga segmentos iguais ao nome do ficheiro (`index.py:303`).
4. Outro processo: escreve o SQLite; `chunk_writes` sai na mesma transacção (`index.py:318`).
5. Falha a meio: o `commit` é no fim (`index.py:320`). Erro desfaz o lote.
6. Ciclo seguinte: sem linhas, não repete.
7. Quem chama: `refreshParentListing` (`index.py:268`) e `listEntries` (`search.py:267`).

### `scheduleParentRefresh` (`index.py:621`)

1. Especificação: TTL ~15s partilhado entre processos (decisão 23). O `lsjson` corre noutro processo (`refresh-parent`). `_claimListingRefresh` (`index.py:382`).
2. Vazio: a chave é o caminho, inclusive `""` para a raiz. O comando leva `--` antes do caminho (`index.py:674`).
3. Identidade: um refresh por caminho dentro do TTL. A fila não repete o mesmo caminho (`index.py:537`).
4. Outro processo: o carimbo e a fila são ficheiros com `flock` (`index.py:370`, `index.py:422`). O dono é o PID mais o `starttime` (`index.py:523`). `_listingBusy` é só do processo que está a listar (`index.py:708`).
5. Falha a meio: se o processo não arranca, a fila larga o caminho e o carimbo sai (`index.py:636`). Se o `lsjson` falha, o carimbo sai no `finally` (`index.py:728`).
6. Ciclo seguinte: carimbo dentro do TTL não relança (`index.py:388`). Dono morto, ou PID reutilizado, volta a arrancar um worker.
7. Quem chama: `listEntries` (`search.py:275`) e `fuse.py` `readdir` (`fuse.py:634`).

### `runParentRefresh` (`index.py:643`) e `_refreshQueuedPath` (`index.py:704`)

1. Especificação: o subprocesso aplica o `lsjson` e avisa o Dolphin se a pasta mudou (decisão 19).
2. Vazio: fila vazia larga o PID (`index.py:598`).
3. Identidade: só o processo cujo PID está na fila tira caminhos (`index.py:595`).
4. Outro processo: o pedido de dono vê o PID do pai negativo ou o `starttime` (`index.py:579`).
5. Falha a meio: a excepção fica no log e o carimbo da pasta sai (`index.py:718`). Os caminhos já tirados da fila não voltam; o TTL seguinte pede outra vez.
6. Ciclo seguinte: o mesmo worker esvazia o que entrou na fila enquanto trabalhava (`index.py:652`).
7. Quem chama: `_cmdRefreshParent` (`cli.py:564`). `_refreshQueuedPath` é chamado por `_drainListingQueue` (`index.py:657`).

### `_keepVanished` (`index.py:2277`)

1. Especificação: alteração local que ainda não subiu não sai quando o item desaparece da nuvem. `error` é tentativa de envio no ciclo seguinte (`syncpins.py:762`).
2. Vazio: estado vazio não fica.
3. Identidade: o estado é o da linha daquele caminho.
4. Outro processo: a decisão é lida na mesma transacção que apaga ou mantém a linha.
5. Falha a meio: não grava sozinha.
6. Ciclo seguinte: `dirty`, `syncing`, `conflict` e `error` continuam no índice. O delete na nuvem marca esses quatro como `conflict` (`index.py:2202`).
7. Quem chama: `_releaseVanishedTree` (`index.py:2258`) e `_dropUnseenRows` (`index.py:2731`).

### `upsertLiveChildren` (`index.py:760`)

1. Especificação: listagem local actualiza o SQLite e não apaga tudo se o `lsjson` vier vazio (`index.py:801`).
2. Vazio: sem filhos e com linhas, não apaga (`index.py:801`).
3. Identidade: `_relocateKnownIds` e o mapa de `itemId` (`index.py:787`). Item com `itemId` que sumiu desta pasta não é apagado (`index.py:813`); pode ter sido movido para fora.
4. Outro processo: uma conexão e um `commit` no fim (`index.py:816`). `_relocateKnownIds` pode ter commitado o move antes.
5. Falha a meio: move já commitado fica; o upsert seguinte volta no próximo refresh.
6. Ciclo seguinte: o mesmo `lsjson` é idempotente pelo `ON CONFLICT(path)`.
7. Quem chama: `refreshParentListing` (`index.py:278`).

### `upsertEntry` (`index.py:870`)

1. Especificação: `put`/`mkdir`/`stat` gravam a linha e preservam `pinned` e a baseline (`index.py:886`, `index.py:909`).
2. Vazio: caminho vazio levanta erro (`index.py:880`).
3. Identidade: conflito é por `path`. `itemId` vazio não apaga o que já está (`index.py:928`).
4. Outro processo: `commit` próprio (`index.py:948`).
5. Falha a meio: um único `INSERT`. Índice único de `itemId` pode falhar se o id já estiver noutra linha.
6. Ciclo seguinte: repetir actualiza a mesma linha.
7. Quem chama: `_statRemoteIntoIndex` (`search.py:301`) e os jobs de mutate.

### `clearItemBaseline` (`index.py:788`)

1. Especificação: pin sem cópia local limpa a baseline para o ciclo descarregar (decisão 22).
2. Vazio: caminho vazio volta (`index.py:788`).
3. Identidade: um `path` só.
4. Outro processo: `commit` (`index.py:796`). Não mexe em `syncState`.
5. Falha a meio: um `UPDATE`.
6. Ciclo seguinte: baseline vazia não é delete de nuvem (`_keepVanished` / sync).
7. Quem chama: `syncpins.py` `_keepPinnedChild`.

### `deleteIndexTree` (`index.py:985`)

1. Especificação: apaga a linha, `file_chunks` e `chunk_writes`. Não apaga a nuvem.
2. Vazio: raiz levanta erro (`index.py:986`).
3. Identidade: `path` e prefixo com escape (`index.py:988`).
4. Outro processo: `commit` no fim (`index.py:1003`).
5. Falha a meio: sem `commit` se alguma das três queries falha.
6. Ciclo seguinte: o delta pode reinserir o item se ele ainda existe na nuvem.
7. Quem chama: mutate de delete local do índice.

### `renameIndexPath` (`index.py:1009`) e `_retargetChunks` (`index.py:954`)

1. Especificação: move de índice e de intervalos. `itemId` mantém-se em `_moveRowPayload` (`index.py:685`).
2. Vazio: origem ou destino vazio levantam erro (`index.py:1010`).
3. Identidade: destino é apagado e reescrito (`index.py:1041`). `_deleteForeignChunks` (`index.py:1052`) tira `file_chunks` e `chunk_writes` do destino que não pertencem à origem, antes do `UPDATE` (`index.py:971`).
4. Outro processo: `chunk_writes` está no SQLite, não só em memória.
5. Falha a meio: o `commit` é depois do retarget (`index.py:1054`). Erro de chave desfaz o rename. Intervalos já apagados no mesmo `commit` voltam atrás.
6. Ciclo seguinte: origem já não existe; o delta casa pelo `itemId`.
7. Quem chama: `mutate.py` e `fuse.py`. `_retargetChunks` também é chamado por `_moveIndexPaths` (`index.py:2298`).

### `_rebuildGraph` (`index.py:1406`)

1. Especificação: delta da raiz e delta próprio da pasta partilhada (decisão 21). `pinned` não é tocado pelo `UPSERT_SQL` (`index.py:66`).
2. Vazio: `deltaLink` vazio enumera tudo. Link novo vazio levanta `GraphDeltaExpired` (`index.py:1434`) e não corre `_dropUnseenRows`.
3. Identidade: mapa `itemId` → caminho (`index.py:1411`).
4. Outro processo: scopes gravados durante o feed, antes do cursor da raiz (`index.py:1436`).
5. Falha a meio: lotes já commitados ficam. Cursor da raiz só avança com zero órfãos (`index.py:1433`).
6. Ciclo seguinte: cursor guardado pede só o que mudou. Scope já gravado repete o delta da pasta se o link ainda for vazio.
7. Quem chama: `_rebuild` (`index.py:1369`).

### `_applyDeltaFeed` (`index.py:1563`)

1. Especificação: a última ocorrência do mesmo `itemId` ganha. Delete seguido de item vivo não apaga (`index.py:1602`).
2. Vazio: item sem id é ignorado em `parseDeltaItem` (`graph.py:34`).
3. Identidade: `deletes.pop` quando a ocorrência seguinte está viva (`index.py:1602`).
4. Outro processo: o flush grava o scope no SQLite antes de devolver.
5. Falha a meio: páginas já flushed ficam. O delete final ainda não correu se a excepção é no meio do feed.
6. Ciclo seguinte: cursor não avança se o feed não chega a `_saveDeltaState` da raiz.
7. Quem chama: `_rebuildGraph` para a raiz (`index.py:1417`) e para cada scope (`index.py:1447`).

### `_flushDeltaPending` (`index.py:1750`)

1. Especificação: caminho pelo `itemId` do pai (`index.py:2038`). O hint descodificado só entra depois de o pai do lote não aparecer (`index.py:2046`).
2. Vazio: caminho vazio fica para a volta seguinte (`index.py:1778`). Sem progresso mesmo com hint, o resto fica órfão (`index.py:1874`).
3. Identidade: o ocupante vai para conflito e o mapa acompanha (`index.py:1807`). O lote grava o caminho final de `ids` (`index.py:1848`). Excluído tira a árvore antiga (`index.py:1784`).
4. Outro processo: `_rememberSharedScope` faz `commit` do scope (`index.py:1740`). O mapa `ids` é deste processo; o SQLite é que o outro lê.
5. Falha a meio: o displace do disco pode ocorrer antes do `commit` do índice (`search.py:80`, `index.py:2134`). O lote que falha não avança o cursor.
6. Ciclo seguinte: órfão mantém o cursor. Item já movido casa `oldPath == itemPath`. Conflito que o tombstone não levou continua no caminho de conflito, com o `itemId`.
7. Quem chama: `_applyDeltaFeed` (`index.py:1588` e `index.py:1626`).

### `_rememberSharedScope` (`index.py:1710`)

1. Especificação: pasta com `remoteItem` tem delta próprio (decisão 21).
2. Vazio: sem drive ou sem id remoto, volta (`index.py:1692`).
3. Identidade: a chave do scope é o `itemId` do atalho (`index.py:1694`).
4. Outro processo: `_saveDeltaState` commit (`index.py:2292`).
5. Falha a meio: se o prefixo e o link já são os mesmos, não regrava (`index.py:1706`).
6. Ciclo seguinte: link vazio enumera a pasta; link antigo continua incremental com o prefixo novo.
7. Quem chama: `_flushDeltaPending` (`index.py:1806`).

### `_flushDeltaDeletes` (`index.py:2170`)

1. Especificação: sumiu da nuvem e tem `baseEtag`, sem `dirty`/`syncing`/`conflict`/`error`, sai do índice e do disco (decisão 22). `_keepVanished` (`index.py:2277`).
2. Vazio: sem linha para o `itemId`, não há vanish (`index.py:2188`).
3. Identidade: o caminho é o da linha actual (`index.py:2191`), não o mapa antigo.
4. Outro processo: scope do atalho sai na mesma passagem (`index.py:2183`). `commit` se houve caminhos (`index.py:2227`).
5. Falha a meio: cópia que não sai mantém a linha (`index.py:2270`). `error` mantido passa a `conflict` com «apagado na nuvem» (`index.py:2202`).
6. Ciclo seguinte: linha mantida volta a ser tentada. O sync trata `conflict` como alteração local (`syncpins.py:762`). Scope apagado não reentra pelo delta da pasta.
7. Quem chama: `_applyDeltaFeed` (`index.py:1926`).

### `_removeVanishedCopies` (`index.py:1995`)

1. Especificação: a linha só sai se a cópia local sai ou já não existe (decisão 22).
2. Vazio: caminho ausente não impede o delete da linha (`index.py:1962`).
3. Identidade: olha `pinned/` e `cache/` daquele path (`index.py:1960`).
4. Outro processo: `rmdir`/`unlink` no disco deste perfil.
5. Falha a meio: um dos dois sítios pode já ter saído e o outro falhar; a linha fica e o próximo ciclo repete.
6. Ciclo seguinte: `rmdir` de pasta vazia conclui e a linha sai.
7. Quem chama: `_dropVanishedPath` (`index.py:1936`). Esse é chamado por `_releaseVanishedTree` e `_dropUnseenRows` (`index.py:2350`).

### `_rewriteDescendantPaths` (`index.py:2140`) e `relocateStoredFiles` (`search.py:61`)

1. Especificação: move por `itemId` leva o ficheiro local e a linha. O ocupante, dirty ou não, sai do caminho antes do replace (`index.py:2130`).
2. Vazio: origem vazia ou igual ao destino volta (`index.py:2146`, `search.py:62`). Sem linha no destino, não há displace (`index.py:2120`).
3. Identidade: o ocupante com outro `itemId` vai inteiro para o conflito, com pin e baseline (`index.py:2133`). Dirty marca `syncState` (`index.py:2136`).
4. Outro processo: o rename do disco acontece antes do `commit` do índice (`search.py:82`, `index.py:2134`).
5. Falha a meio: se o índice falha depois do rename, o ficheiro está no destino e a linha ainda na origem até ao próximo delta.
6. Ciclo seguinte: o delta volta a resolver pelo `itemId`, inclusive a partir do conflito.
7. Quem chama: `_flushDeltaPending` (`index.py:1807`), `upsertLiveChildren` (`index.py:532`) e `_relocateKnownIds` (`index.py:2327`).

### `_resolveDeltaPath` (`index.py:2019`)

1. Especificação: `itemId` do pai é a identidade (`docs/DECISIONS.md` decisão 21). O hint é reserva.
2. Vazio: sem pai no mapa e sem hint, devolve `""` (`index.py:2055`).
3. Identidade: pai já no mapa ganha ao hint (`index.py:2038`). Pai ainda no lote não usa hint (`index.py:2048`).
4. Outro processo: não grava. O mapa é o do ciclo.
5. Falha a meio: não altera disco nem índice.
6. Ciclo seguinte: órfão volta porque o cursor não avança.
7. Quem chama: `_flushDeltaPending` (`index.py:1770`).

### `_displaceOccupiedTree` (`index.py:2105`)

1. Especificação: dois itens não ficam no mesmo caminho; o que já tem baseline não perde o `itemId` antes do tombstone (decisão 21 e 22).
2. Vazio: destino sem linha volta `""` (`index.py:2120`).
3. Identidade: o `itemId` do ocupante muda de caminho, não é apagado (`index.py:2134`).
4. Outro processo: o índice commitado é que o sync lê.
5. Falha a meio: `relocateStoredFiles` pode já ter movido o disco se `_moveIndexPaths` falhar.
6. Ciclo seguinte: tombstone lê o caminho actual (`index.py:1896`). Sem tombstone, a linha fica no conflito.
7. Quem chama: `_rewriteDescendantPaths` (`index.py:2150`).

### `_dropUnseenRows` (`index.py:2412`)

1. Especificação: só num ciclo completo sem órfãos (`index.py:1505`). Não apaga dirty nem baseline vazia (`index.py:2339`).
2. Vazio: `seen is None` volta (`index.py:2316`).
3. Identidade: caminho visto fica. Filho de um mantido também fica (`index.py:2348`).
4. Outro processo: `commit` no fim (`index.py:2351`).
5. Falha a meio: `_removeVanishedCopies` pode ter apagado um disco e mantido a linha.
6. Ciclo seguinte: linha mantida entra outra vez se continuar invisível e já tiver baseline.
7. Quem chama: `_rebuildGraph` (`index.py:1506`) e `_deleteUnseen` (`index.py:2372`).

### `listEntries` (`search.py:262`)

1. Especificação: SQLite imediato; sem índice, espera o `lsjson` (decisão 19). Não emite `.`.
2. Vazio: pai que é ficheiro sem descendentes devolve lista vazia (`search.py:268`).
3. Identidade: a lista é `WHERE parent = ?` (`search.py:252`).
4. Outro processo: o refresh é o subprocesso `refresh-parent` (`index.py:621`).
5. Falha a meio: erro de `lsjson` na primeira vez usa o que já estiver no índice (`search.py:280`).
6. Ciclo seguinte: pasta conhecida só agenda refresh (`search.py:275`).
7. Quem chama: o worker KIO `listDir`.

## Releitura desta passagem

Corrigidos e relidos: `_keepVanished`, `_releaseVanishedTree`, `_dropUnseenRows`, `_flushDeltaDeletes`, `_listingOwnerAlive`, `_enqueueListingPath`, `_publishListingWorker`, `_claimListingWorker`, `scheduleParentRefresh`, `runParentRefresh`, `_refreshQueuedPath`. `rowToDict` (`search.py:153`) foi relido porque o ficheiro mudou: a listagem passa a pôr em `localPath` o cache completo sujo, à frente do pin. Quem chama está nas linhas acima. `error` já não perde a cópia local quando o item sai da nuvem. Um PID reutilizado já não fica dono da fila. A releitura não acrescentou achado em aberto.

## pin, skipPin, hydrate, sync-pins

Funções que mudam índice, disco ou remoto, relidas depois da correcção: `pinRemote`, `_pinRemote`, `unpinRemote`, `_unpinRemote`, `freeLocalSpace`, `_baselineAfterPin`, `_syncOrRefuse`, `_flushPendingOrRefuse`, `_pinDirectory`, `_pinCopiedTree`, `_removePinnedFiles`, `_restorePinnedStillPresent`, `_pruneEmptyParents`, `hydrateRemote`, `_hydrateRemote`, `_fillDirtyPartial`, `adoptDownloadedFile`, `deleteChunkRow`, `prepareSparse`, `noteWrite`, `ensureRange`, `fillHoles`, `_fillSpan`, `_saveRow`, `_storePendingWrite`, `_dropPending`, `syncPins`, `_syncPinsLocked`, `_syncPinnedRows`, `_syncOnePath`, `_syncPinnedDirectory`, `_applyRemoteListing`, `_syncMissingPinnedDirectory`, `_downloadPinnedChildren`, `_preferDirtyCache`, `_promoteDirtyCache`, `_resolveHashedBaseline`, `_keepPinnedChild`, `_deleteEquivalentRemote`, `_flushPendingRmdirs`, `_rmdirDeferred`, `_dropRemovedDir`, `_resolveBothExist`, `_resolveRemoteOnly`, `_resolveLocalOnly`, `_adoptOrSend`, `_uploadLocal`, `_downloadRemote`, `_downloadLocked`, `_writeConflict`, `_syncDirtyCache`, `_materializePinFromCache`, `_intactLocal`, `_ensureFilled`, `_removeLocalCopies`, `scheduleItemSync`.

`storedPinFlag` não grava. Chama `isPinnedHereOrAbove` (`pin.py:245`). Quem grava a flag: `fuse.py` `create` (`fuse.py:216`), `_renameAtomicSave` (`fuse.py:509`), `_renameUntracked` (`fuse.py:537`); `mutate.py` `_indexRemoteTree` (`mutate.py:375`), `_indexAfterWrite` (`mutate.py:401`), `mkdirRemotePath` (`mutate.py:103`).

### `pinRemote` (`pin.py:43`) e `_pinRemote` (`pin.py:60`)

1. Especificação: cópia para `pinned/` e `pinned=1`; a raiz e o `skipPin` são recusados (decisão 14 e 19).
2. Vazio: caminho vazio levanta erro (`pin.py:63`).
3. Identidade: um `itemPath` por vez. O diretório entra por staging (`pin.py:318`).
4. Outro processo: `pinsLock` (`pin.py:44`) e `cacheJobsLock` (`pin.py:85`).
5. Falha a meio: o staging sai (`pin.py:339`). A baseline só foi limpa se ainda não havia cópia (`pin.py:83`).
6. Ciclo seguinte: `_baselineAfterPin` (`pin.py:102`) ou o sync comparam o ficheiro já no disco.
7. Quem chama: o CLI `pin`.

### `unpinRemote` (`pin.py:48`) e `_unpinRemote` (`pin.py:106`)

1. Especificação: tira a cópia local e não apaga a nuvem (decisão 19).
2. Vazio: a raiz levanta erro (`pin.py:109`).
3. Identidade: a árvore é a do `itemPath` (`pin.py:123`).
4. Outro processo: o sync de alterações corre antes do `pinsLock` (`pin.py:51`).
5. Falha a meio: a flag volta a `pinned=1` no que ainda está no disco (`pin.py:129`, `pin.py:140`).
6. Ciclo seguinte: sem flag e sem pasta, o sync não recria o pin. Filho de uma pasta ainda pinada volta a ser pinado em `_keepPinnedChild`.
7. Quem chama: CLI `unpin` e `freeLocalSpace` (`pin.py:168`).

### `freeLocalSpace` (`pin.py:156`)

1. Especificação: unpin, ou apaga `cache/` se o item não está pinado (`README.md`).
2. Vazio: a raiz levanta erro (`pin.py:163`). Sem cópia local, levanta erro (`pin.py:182`).
3. Identidade: um `itemPath`.
4. Outro processo: o sync corre primeiro se há alterações (`pin.py:167`, `pin.py:172`).
5. Falha a meio: se o sync deixa `dirty`, nada é apagado (`pin.py:173`).
6. Ciclo seguinte: sem cache e sem pin, não há upload.
7. Quem chama: CLI `free-space`.

### `_baselineAfterPin` (`pin.py:185`)

1. Especificação: o pin acabado de descarregar adopta `baseEtag` de conteúdo (decisão 22).
2. Vazio: sem linha ou sem ficheiro, volta (`pin.py:189`).
3. Identidade: cada filho pelo `Path` do `lsjson` (`pin.py:200`).
4. Outro processo: corre dentro do `pinsLock` do `pinRemote`.
5. Falha a meio: o erro fica no log (`pin.py:205`). A baseline que faltou é resolvida no sync.
6. Ciclo seguinte: baseline vazia entra em `_resolveHashedBaseline`.
7. Quem chama: `_pinRemote` (`pin.py:102`).

### `_flushPendingOrRefuse` (`pin.py:389`) e `_syncOrRefuse` (`pin.py:233`)

1. Especificação: não descartar alterações locais no unpin nem no libertar espaço.
2. Vazio: sem `dirty`, não chama o sync (`pin.py:390`).
3. Identidade: a árvore de `listDescendants` (`pin.py:401`).
4. Outro processo: `syncPins` pede o `pinsLock`.
5. Falha a meio: se continua `dirty`, a operação levanta erro e não apaga (`pin.py:395`).
6. Ciclo seguinte: o mesmo sync volta a tentar o envio.
7. Quem chama: `unpinRemote` (`pin.py:51`) e `freeLocalSpace` (`pin.py:167`).

### `_pinDirectory` (`pin.py:311`) e `_pinCopiedTree` (`pin.py:351`)

1. Especificação: a pasta pinada fica em `pinned/` e o `skipPin` não entra (decisão 14).
2. Vazio: o staging é recriado (`pin.py:319`).
3. Identidade: o caminho relativo ao root do pin (`pin.py:358`).
4. Outro processo: dentro de `cacheJobsLock`.
5. Falha a meio: o staging sai (`pin.py:340`). O que já foi para o lugar fica.
6. Ciclo seguinte: a pasta com `pinned=1` entra no sync.
7. Quem chama: `_pinRemote` (`pin.py:88`).

### `_removePinnedFiles` (`pin.py:412`) e `_restorePinnedStillPresent` (`pin.py:424`)

1. Especificação: o unpin apaga a árvore local. Se a remoção falha, a flag acompanha o que ficou.
2. Vazio: diretório ausente volta (`pin.py:416`).
3. Identidade: o filho é `itemPath/nome` (`pin.py:419`).
4. Outro processo: dentro do `pinsLock`.
5. Falha a meio: `rmdir` a meio deixa a flag reposta só nos caminhos ainda presentes (`pin.py:426`).
6. Ciclo seguinte: o que ficou com `pinned=1` volta a sincronizar.
7. Quem chama: `_unpinRemote` (`pin.py:128`).

### `_hydrateRemote` (`hydrate.py:38`)

1. Especificação: até ao limiar, `cache/` gravável; não substitui cache `dirty` (decisão 22 e 24).
2. Vazio: a raiz e uma pasta levantam erro (`hydrate.py:42`, `hydrate.py:45`).
3. Identidade: se `pinned/` já tem o ficheiro, devolve esse (`hydrate.py:47`).
4. Outro processo: `cacheJobsLock` (`hydrate.py:34`).
5. Falha a meio: o parcial do `copyToReplace` sai. O cache `dirty` não foi substituído (`hydrate.py:63`).
6. Ciclo seguinte: tamanho e mtime iguais reutilizam o cache (`hydrate.py:71`).
7. Quem chama: CLI, KRunner, FUSE `open` e as definições.

### `adoptDownloadedFile` (`hydrate.py:140`)

1. Especificação: grava a baseline de conteúdo e alinha o mtime. Não rebobina um `cache/` com alterações (decisão 22).
2. Vazio: sem token, volta depois de alinhar o mtime (`hydrate.py:163`).
3. Identidade: um `itemPath`. O cache ao lado fica de fora se `protectDirtyCache` (`hydrate.py:155`).
4. Outro processo: `setBaseline` faz `commit`.
5. Falha a meio: o mtime pode já ter mudado e a baseline não. O sync vê o mtime e envia.
6. Ciclo seguinte: token igual não volta a descarregar.
7. Quem chama: hydrate, pin, sync e `mutate.py` `_indexAfterWrite` (`mutate.py:419`).

### `noteWrite` (`chunks.py:115`) e `_fillSpan` (`chunks.py:201`)

1. Especificação: o intervalo escrito fica em `chunk_writes` para outro processo não o encher com o remoto (decisão 22). O `mtime` anterior é reposto (`chunks.py:263`).
2. Vazio: caminho vazio ou comprimento nulo voltam (`chunks.py:116`).
3. Identidade: a chave é o `path` do item.
4. Outro processo: a linha vai para o SQLite (`chunks.py:373`). O cadeado dos blocos é só deste processo (`chunks.py:283`). O write guard do FUSE impede o sync enquanto o handle está aberto.
5. Falha a meio: o `pwrite` pode ter ocorrido e o `INSERT` não. O processo que escreveu ainda tem o intervalo em memória (`chunks.py:369`).
6. Ciclo seguinte: bitmap completo apaga a linha (`chunks.py:259`). Buraco que ficou volta a ser preenchido.
7. Quem chama: FUSE `write` (`fuse.py:264`) e `fillHoles` (`chunks.py:197`).

### `prepareSparse` (`chunks.py:72`) e `deleteChunkRow` (`chunks.py:60`)

1. Especificação: etag ou tamanho diferentes apagam o sparse, excepto se o cache está `dirty` (decisão 24).
2. Vazio: o ficheiro é criado com o tamanho remoto (`chunks.py:91`).
3. Identidade: uma linha `file_chunks` por caminho.
4. Outro processo: `commit` em `_saveRow` (`chunks.py:326`).
5. Falha a meio: `dirty` devolve o ficheiro actual sem trocar o bitmap (`chunks.py:77`).
6. Ciclo seguinte: a mesma etag reutiliza a linha (`chunks.py:76`).
7. Quem chama: o `open` do FUSE. `deleteChunkRow` é chamado pelo hydrate, pelo fill e pelo rename do índice.

### `syncPins` (`syncpins.py:72`) e `_syncPinsLocked` (`syncpins.py:88`)

1. Especificação: 3 vias só em `pinned=1`; cache `dirty` só sobe; sem sync da raiz (decisão 19 e 22).
2. Vazio: sem caminho percorre os pins (`syncpins.py:112`). Caminho efémero volta já (`syncpins.py:104`).
3. Identidade: um perfil por invocação, debaixo do `pinsLock`.
4. Outro processo: `pinsLock` e `cacheJobsLock` (`syncpins.py:76`, `syncpins.py:84`).
5. Falha a meio: a excepção marca actividade de erro e sobe (`syncpins.py:116`). Lotes já commitados ficam.
6. Ciclo seguinte: `dirty`, `error` e `syncing` voltam a ser tentativa de envio.
7. Quem chama: CLI, watcher (`watcher.py:68`), unpin e libertar espaço.

### `_syncOnePath` (`syncpins.py:137`)

1. Especificação: pasta pinada sincroniza a árvore; ficheiro segue a 3 vias; cache `dirty` sem pin só sobe (`syncpins.py:178`).
2. Vazio: remoto ausente e local ausente contam como skipped (`syncpins.py:190`).
3. Identidade: a linha actual do `itemPath`.
4. Outro processo: o stat remoto falhado deixa `error` (`syncpins.py:160`).
5. Falha a meio: `blocked` volta sem apagar (`syncpins.py:153`).
6. Ciclo seguinte: `error` conta como alteração local (`syncpins.py:734`).
7. Quem chama: `_syncPinsLocked` (`syncpins.py:109`), `_syncPinnedRows` (`syncpins.py:134`) e `_preferDirtyCache` (`syncpins.py:344`).

### `_keepPinnedChild` (`syncpins.py:490`)

1. Especificação: filho de pasta pinada fica `pinned=1`. Sem cópia em `pinned/`, a baseline antiga não pode autorizar o apagar da nuvem (decisão 22).
2. Vazio: `skipPin` volta (`syncpins.py:491`).
3. Identidade: já pinado chama `_ensurePinnedCopy` (`syncpins.py:521`). Sem `pinned/` e sem cache, a baseline sai.
4. Outro processo: `upsertPinned` e `clearItemBaseline` fazem `commit`.
5. Falha a meio: a flag pode ficar `1` e a baseline vazia. O ciclo descarrega.
6. Ciclo seguinte: baseline vazia e sem ficheiro descarrega (`syncpins.py:667`).
7. Quem chama: `_applyRemoteListing` (`syncpins.py:256`).

### `_materializePinFromCache` (`syncpins.py:1057`) e `_intactLocal` (`syncpins.py:1097`)

1. Especificação: a cópia do pin é `pinned/`. `cache/` íntegro não substitui essa cópia, para a eviction não parecer um apagar local (decisão 19).
2. Vazio: sem cache, ou parcial, devolve `None` e o chamador trata de `missing` ou `download` (`syncpins.py:1064`, `syncpins.py:1122`).
3. Identidade: não escreve por cima de um `pinned/` que já é ficheiro (`syncpins.py:1071`).
4. Outro processo: a cópia corre com o exclusivo (`syncpins.py:1068`). Sem o exclusivo, o ciclo não apaga.
5. Falha a meio: o staging sai (`syncpins.py:1086`). Sem `pinned/`, não há delete da nuvem neste ciclo.
6. Ciclo seguinte: `pinned/` presente segue a 3 vias. Sem `pinned/` e sem cache, `_ensurePinnedCopy` esvazia a baseline e o ciclo descarrega.
7. Quem chama: `_intactLocal` é chamado por `_syncOnePath` (`syncpins.py:152`).

### `_resolveRemoteOnly` (`syncpins.py:655`) e `_deleteEquivalentRemote` (`syncpins.py:524`)

1. Especificação: pin sem cópia e sem baseline descarrega. Pin com baseline e conteúdo igual apaga a nuvem (decisão 22). Pasta faz `rmdir` no fim, da mais funda para a raiz.
2. Vazio: sem `pinned=1`, skipped (`syncpins.py:663`). Baseline vazia descarrega (`syncpins.py:667`).
3. Identidade: `mt:` e `qxh:` com a mesma data e tamanho contam como o mesmo conteúdo (`syncpins.py:504`).
4. Outro processo: o delete remoto precede o `deleteIndexTree` (`syncpins.py:540`).
5. Falha a meio: o remoto pode já ter saído e a linha ficar. O ciclo seguinte, sem remoto e sem local, não repete o delete (`syncpins.py:190`).
6. Ciclo seguinte: conteúdo diferente tira o pin e não apaga a nuvem (`syncpins.py:698`).
7. Quem chama: `_syncOnePath` (`syncpins.py:184`) e `_syncMissingPinnedDirectory` (`syncpins.py:288`). `_flushPendingRmdirs` (`syncpins.py:550`) chama `_rmdirDeferred`.

### `_ensurePinnedCopy` (`syncpins.py:491`)

1. Especificação: pin sem `pinned/` e sem cache limpa a baseline para o ciclo descarregar. Cache íntegro e não dirty é copiado para `pinned/` antes (decisão 22).
2. Vazio: sem linha, sem `pinned=1` ou pasta volta (`syncpins.py:493`). Sem as duas cópias, `clearItemBaseline` (`syncpins.py:513`).
3. Identidade: um `itemPath`. Cache parcial ou dirty não é copiado para `pinned/` (`syncpins.py:499`).
4. Outro processo: `_materializePinFromCache` pede o exclusivo (`syncpins.py:1096`). `clearItemBaseline` faz `commit`.
5. Falha a meio: staging do materialize sai. Se o cache continua no disco, a baseline não é limpa (`syncpins.py:511`).
6. Ciclo seguinte: baseline vazia e remoto presente descarrega (`syncpins.py:695`). Com cache dirty, a promoção trata o envio.
7. Quem chama: `_syncOnePath` (`syncpins.py:150`) e `_keepPinnedChild` (`syncpins.py:522`).

### `_uploadLocal` (`syncpins.py:835`)

1. Especificação: alteração local sobe. Cache `dirty` com `pinned/` substitui essa cópia antes do envio (decisão 22). Buracos recusam o envio (`syncpins.py:858`).
2. Vazio: nome efémero volta (`syncpins.py:841`).
3. Identidade: o exclusivo é pedido depois da promoção, não durante ela (`syncpins.py:848`, `syncpins.py:854`).
4. Outro processo: se o exclusivo falha, skipped (`syncpins.py:850`, `syncpins.py:856`).
5. Falha a meio: `syncing` passa a `error` (`syncpins.py:891`). O remoto pode já ter o ficheiro.
6. Ciclo seguinte: `error` volta a contar como alteração local e o envio repete.
7. Quem chama: `_syncOnePath` (`syncpins.py:179`), `_resolveBothExist` (`syncpins.py:647`), `_resolveLocalOnly` (`syncpins.py:711`, `syncpins.py:717`), `_writeConflict` (`syncpins.py:1020`) e `_syncDirtyCache` (`syncpins.py:1033`, `syncpins.py:1053`).

### `_resolveHashedBaseline` (`syncpins.py:409`) e `_resolveBothExist` (`syncpins.py:595`)

1. Especificação: baseline vazia, `migrated:` ou `mt:` comparam o QuickXor. Iguais adoptam `qxh:`. Diferentes guardam o conflito e descarregam o remoto. QuickXor inconclusivo com mtime próximo fica `error` e não adopta (decisão 22).
2. Vazio: parcial `dirty` é preenchido antes do hash (`syncpins.py:416`).
3. Identidade: o mesmo `itemPath` para a cópia local e o stat remoto.
4. Outro processo: o conflito e o download pedem o exclusivo.
5. Falha a meio: o ficheiro de conflito pode já existir e o download não. O estado fica `error` ou `dirty`.
6. Ciclo seguinte: baseline `qxh:` segue a comparação de token (`syncpins.py:620`).
7. Quem chama: `_resolveBothExist` (`syncpins.py:612`). Esse é chamado por `_syncOnePath` (`syncpins.py:181`).

### `_promoteDirtyCache` (`syncpins.py:350`) e `_ensureFilled` (`syncpins.py:1142`)

1. Especificação: `cache/` com alterações substitui `pinned/` e o duplicado sai. Preencher buracos repõe o mtime e não conta como edição (decisão 22).
2. Vazio: sem cache ou sem `dirty`, skip (`syncpins.py:358`).
3. Identidade: o mesmo `itemPath`. Se `pinned/` é pasta, o cache não é copiado para cima dela (`syncpins.py:366`).
4. Outro processo: a cópia tem exclusivo (`syncpins.py:354`). Quem já o tem recebe `blocked`.
5. Falha a meio: o cache só sai depois de `pinned/` existir (`syncpins.py:376`).
6. Ciclo seguinte: `blocked` ou buraco deixam `dirty` (`syncpins.py:1175`).
7. Quem chama: `_intactLocal` (`syncpins.py:1105`), `_preferDirtyCache` (`syncpins.py:339`) e `_uploadLocal` (`syncpins.py:848`).

### `_writeConflict` (`syncpins.py:978`) e `_downloadLocked` (`syncpins.py:930`)

1. Especificação: conflito guarda a cópia local e descarrega o remoto (decisão 22). Hydrate e download não substituem cache `dirty`.
2. Vazio: sem exclusivo, skipped (`syncpins.py:988`).
3. Identidade: o nome do conflito leva a data (`syncpins.py:993`).
4. Outro processo: o download do original corre com o exclusivo já tomado (`syncpins.py:1017`). O envio do conflito pede o exclusivo dele.
5. Falha a meio: a cópia de conflito pode ficar sem o download do remoto. `error` no item original.
6. Ciclo seguinte: o conflito está `dirty` e o envio repete (`syncpins.py:1011`).
7. Quem chama: `_resolveHashedBaseline`, `_resolveBothExist` e `_adoptOrSend`.

### `scheduleItemSync` (`syncpins.py:1190`)

1. Especificação: `IN_CLOSE_WRITE` / `IN_MOVED_TO` marcam `dirty` e disparam `sync-pins --path` (AGENTS.md).
2. Vazio: caminho vazio ou efémero voltam (`syncpins.py:1191`).
3. Identidade: um `itemPath`.
4. Outro processo: `markDirty` faz `commit`. O debounce está no processo do FUSE.
5. Falha a meio: a flag `dirty` pode ficar sem o sync ter corrido. O timer volta a apanhar.
6. Ciclo seguinte: `_uploadLocal` envia.
7. Quem chama: FUSE `release` e o rename local.

## Releitura desta passagem

Corrigidos e relidos: `storedPinFlag` e quem o chama em `fuse.py` e `mutate.py`; `_uploadLocal` e quem o chama; `_keepPinnedChild`, `_ensurePinnedCopy`, `_materializePinFromCache` e `_intactLocal`. Criar um ficheiro fora do `skipPin` já não levanta `NameError`. O envio de um pin pede o exclusivo uma vez. Um pin já marcado, sem `pinned/` e sem cache, esvazia a baseline para o ciclo descarregar. Cache íntegro é copiado para `pinned/` antes. A releitura de `_ensurePinnedCopy` não acrescentou achado em aberto.

## FUSE e watcher

Funções que mudam índice, disco ou remoto, relidas depois da correcção: `readdir`, `open`, `create`, `write`, `truncate`, `release`, `unlink`, `mkdir`, `rmdir`, `rename`, `_renameAtomicSave`, `_renameUntracked`, `chmod`, `debounceSync`, `_readEvents`, `_scheduleMovedTree`, `_watchLoop`, `evictCache`, `_evictionBlocked`, `swapIntoPlace`, `suppressPath`.

`getattr` não grava. Lê o disco ou o SQLite (`fuse.py:87`, `fuse.py:95`).

### `readdir` (`fuse.py:97`)

1. Especificação: nomes do SQLite mais o que já está em `pinned/` e `cache/`. Temporários não entram (AGENTS.md).
2. Vazio: pasta sem índice e sem disco devolve só `.` e `..` (`fuse.py:120`).
3. Identidade: o conjunto é por nome (`fuse.py:100`).
4. Outro processo: o refresh é o TTL partilhado (`fuse.py:99`).
5. Falha a meio: erro a ler o disco salta essa pasta (`fuse.py:118`).
6. Ciclo seguinte: o TTL volta a pedir o `lsjson`.
7. Quem chama: o kernel, no mount.

### `open` (`fuse.py:122`) e `release` (`fuse.py:335`)

1. Especificação: até `openMaxBytes`, hydrate; acima, sparse. Miniatura sem cópia local não hidrata. `release` com buracos fica `dirty` e não agenda sync (decisão 22 e 24).
2. Vazio: sem índice, `ENOENT` (`fuse.py:150`). Pasta, `EISDIR` (`fuse.py:152`).
3. Identidade: `pinned/` completo ganha ao cache (`fuse.py:706`).
4. Outro processo: escrita toma o write guard partilhado (`fuse.py:126`). O sync pede o exclusivo.
5. Falha a meio: o guard fecha no `except` (`fuse.py:200`). O sparse `dirty` não é apagado.
6. Ciclo seguinte: `release` com ficheiro completo agenda o sync (`fuse.py:390`). Com buracos, não (`fuse.py:386`).
7. Quem chama: o kernel. `read` de um parcial chama `ensureRange` (`fuse.py:244`).

### `create` (`fuse.py:204`)

1. Especificação: ficheiro novo no backing do pin ou do cache, linha no índice, `dirty`.
2. Vazio: efémero não grava índice (`fuse.py:211`).
3. Identidade: `storedPinFlag` decide o pin (`fuse.py:218`).
4. Outro processo: o write guard fica no handle até ao `release`.
5. Falha a meio: sem ficheiro, sem linha anterior e sem filhos, a linha nova sai (`fuse.py:226`).
6. Ciclo seguinte: com ficheiro, o `release` envia.
7. Quem chama: o kernel.

### `truncate` (`fuse.py:284`)

1. Especificação: mudar o tamanho é edição local e sobe no sync. Parcial sem `pinned/` preenche buracos antes (decisão 22).
2. Vazio: sem cópia, a miniatura recebe `EACCES` (`fuse.py:314`). Os outros criam o backing (`fuse.py:316`).
3. Identidade: com handle, trunca esse fd (`fuse.py:288`). Sem handle, trunca o ficheiro local.
4. Outro processo: o guard fecha antes do `scheduleItemSync` (`fuse.py:321`, `fuse.py:324`).
5. Falha a meio: a excepção não agenda sync. `dirty` pode já estar marcado.
6. Ciclo seguinte: sem buracos, o sync envia o tamanho novo. Com buracos, fica `dirty` e não agenda (`fuse.py:322`).
7. Quem chama: o kernel. Com handle, o `release` é que agenda.

### `unlink` (`fuse.py:392`), `mkdir` (`fuse.py:412`) e `rmdir` (`fuse.py:433`)

1. Especificação: `unlink` apaga a nuvem. `mkdir` cria primeiro no remoto. `rmdir` é `rclone rmdir` e falha se há filhos locais ou no índice (decisão 22).
2. Vazio: `rmdir` da raiz é `EPERM` (`fuse.py:435`). Efémero só mexe no disco (`fuse.py:394`, `fuse.py:440`).
3. Identidade: um `itemPath`.
4. Outro processo: o remoto corre no CLI, com `cacheJobsLock` do mutate.
5. Falha a meio: `rmdir` remoto falhado recria a pasta local (`fuse.py:463`). `mkdir` local falhado não apaga o remoto já criado.
6. Ciclo seguinte: a linha que ficou volta a aparecer na listagem.
7. Quem chama: o kernel.

### `rename` (`fuse.py:476`)

1. Especificação: temporário de editor só troca o ficheiro e marca `dirty`. Sem `itemId`/`baseEtag`, só o índice. Se já está na nuvem, `moveto` primeiro (decisão 22).
2. Vazio: origem igual ao destino volta (`fuse.py:479`).
3. Identidade: `_indexedOnCloud` olha `itemId` ou `baseEtag` (`fuse.py:660`).
4. Outro processo: o `moveto` é o do mutate. O save atómico não chama `deletefile`.
5. Falha a meio: o `replace` local pode já ter ocorrido e o índice não (`fuse.py:535`).
6. Ciclo seguinte: `scheduleItemSync` envia o destino (`fuse.py:522`, `fuse.py:550`).
7. Quem chama: o kernel.

### `chmod` (`fuse.py:554`)

1. Especificação: grava `unixMode` e aplica no disco se a cópia existe. Não marca `dirty` nem chama o rclone (decisão 25).
2. Vazio: sem linha e sem cópia, volta (`fuse.py:561`).
3. Identidade: um `itemPath`.
4. Outro processo: `chmodItem` faz `commit`. Não há sync.
5. Falha a meio: o SQLite pode ter o modo e o disco não, ou o contrário se o `chmodItem` falha e o fallback corre.
6. Ciclo seguinte: o modo fica. Não há upload.
7. Quem chama: o kernel.

### `debounceSync` (`watcher.py:44`) e `_readEvents` (`watcher.py:129`)

1. Especificação: `IN_CLOSE_WRITE` e `IN_MOVED_TO` disparam `sync-pins --path`. `.goutputstream-*` no nome final sincroniza. `.fod-staging-*`, `.fod-partial` e `.fod-bak-*` não (AGENTS.md).
2. Vazio: caminho vazio ou efémero não arma o timer (`watcher.py:45`).
3. Identidade: um timer por caminho (`watcher.py:55`).
4. Outro processo: `markDirty` é commit. O sync corre 0,4 s depois, noutro thread.
5. Falha a meio: o sync falhado fica no log (`watcher.py:70`). `dirty` mantém-se.
6. Ciclo seguinte: o timer de sync, ou outro close, volta a tentar.
7. Quem chama: `_readEvents` (`watcher.py:184`) e `scheduleItemSync`.

### `_scheduleMovedTree` (`watcher.py:187`)

1. Especificação: mover uma pasta sem pin e sem pasta em `pinned/` não descarrega a árvore (decisão 22).
2. Vazio: caminho que não cabe em `pinned/` nem em `cache/` é ignorado (`watcher.py:198`).
3. Identidade: cada ficheiro da árvore é um `itemPath`.
4. Outro processo: cada um marca `dirty` e o sync é o de sempre.
5. Falha a meio: os ficheiros já marcados ficam `dirty`. Os seguintes não.
6. Ciclo seguinte: o sync da pasta sem `pinned=1` e sem diretório local não corre (`syncpins.py:146`).
7. Quem chama: `_readEvents` num `MOVED_TO` de diretório (`watcher.py:173`).

### `_watchLoop` (`watcher.py:81`)

1. Especificação: uma excepção não fecha o inotify.
2. Vazio: `inotify_init1` falhado regista e volta (`watcher.py:84`).
3. Identidade: as watches são o mapa wd → pasta deste processo.
4. Outro processo: o fd é deste processo.
5. Falha a meio: o erro fica no log e o ciclo continua (`watcher.py:96`).
6. Ciclo seguinte: os eventos novos continuam a ser lidos.
7. Quem chama: `startLocalWatch`, no arranque do FUSE (`fuse.py:645`).

### `evictCache` (`disk.py:308`) e `_evictionBlocked` (`disk.py:346`)

1. Especificação: eviction de 8 GiB soma `st_blocks * 512`, salta `dirty` e ficheiro com `flock` (decisão 24).
2. Vazio: sem `cache/`, volta (`disk.py:314`).
3. Identidade: o protegido é um caminho (`disk.py:332`).
4. Outro processo: o `flock` exclusivo não bloqueante vê outro fd (`disk.py:355`). Abre para leitura, para um modo `0444` não parecer ocupado (`disk.py:349`).
5. Falha a meio: o unlink pode já ter ocorrido e a linha de chunks não. O próximo ciclo recalcula o total.
6. Ciclo seguinte: o que ficou acima do tecto volta a ser candidato.
7. Quem chama: hydrate e `_syncDirtyCache`.

### `swapIntoPlace` (`disk.py:223`)

1. Especificação: a troca do pin é atómica no mesmo disco. Se falha, o alvo anterior volta.
2. Vazio: sem alvo, o staging passa a ser o alvo (`disk.py:236`).
3. Identidade: o backup é `.fod-bak-` ao lado (`disk.py:228`).
4. Outro processo: quem chama já tem o lock do pin ou do cache.
5. Falha a meio: o backup volta ao alvo se o rename do staging falha (`disk.py:238`). O staging sai (`disk.py:246`).
6. Ciclo seguinte: o alvo é o conteúdo novo, ou o antigo se a troca falhou.
7. Quem chama: `_pinDirectory`.

## Releitura desta passagem

Corrigidos e relidos: `readdir`, `create`, `truncate` e `_evictionBlocked`. A listagem já não reintroduz temporários. `truncate` sem handle agenda o sync quando o ficheiro deixa de ter buracos. `create` falhado não deixa linha nova sem ficheiro. A eviction consegue abrir um cache sem bit de escrita para testar o `flock`. A releitura não acrescentou achado em aberto.

## Delete, cópia grande, worker KIO

Funções que mudam índice, disco ou remoto, relidas depois da correcção: `putLocalToRemote`, `mkdirRemotePath`, `removeRemotePath`, `moveRemotePath`, `_moveAcrossProfiles`, `_preserveDirtyDestination`, `_finishPreserved`, `_undoPreserve`, `_indexShelvedCopy`, `_indexRemoteTree`, `_adoptMirroredFiles`, `_indexAfterWrite`, `_retireReplacedCaches`, `_clearPathDirty`, `_dropReplacedCacheFile`, `_mirrorPinnedLocal`, `_removeLocalCopies`, `_moveLocalCopies`, `copyDriveToDrive`, `_dropReplacedLocal`, `_copyRemoteToLocal`, `_copyDirectory`, `detachLocalIdentity`, `copy`, `put`, `mkdir`, `del`, `rename`, `get`, `copyLocalPath`, `rowToDict`, `chmodItem`.

`_refuseDestination` não grava. Recusa destino sujo ou com cópia local quando não há `--overwrite` (`mutate.py:48`). `_prepareProfile` não grava disco; para `copy` e `mv` com dois perfis não activa perfil (`cli.py:439`).

### `putLocalToRemote` (`mutate.py:100`)

1. Especificação: `put` é `rclone copyto` mais índice; se o pai está pinado, também escreve em `pinned/` (`docs/ARCHITECTURE.md`). Não descarta alterações locais do destino.
2. Vazio: origem que não é ficheiro nem pasta levanta erro (`mutate.py:107`). Caminho vazio ou `..` levanta erro (`mutate.py:92`). Nome efémero volta sem enviar (`mutate.py:112`).
3. Identidade: se a origem já é a cópia local do destino, essa cópia é o conteúdo a enviar (`mutate.py:116`). Outra origem, com `--overwrite` e destino sujo, vai para um nome de conflito (`mutate.py:124`).
4. Outro processo: `cacheJobsLock` (`mutate.py:114`). O sync do conflito espera esse lock.
5. Falha a meio: se o `copyto` falha, `_undoPreserve` devolve a cópia ao caminho (`mutate.py:133`). Índice e mirror só correm depois do envio (`mutate.py:135`).
6. Ciclo seguinte: o conflito, já sem `itemId`, é enviado como ficheiro novo. Pasta pinada fica com os ficheiros em `pinned/` e a baseline do stat remoto (`mutate.py:140`).
7. Quem chama: CLI `put` (`cli.py:995`). O worker `put` e a cópia de um ficheiro local para o Local passam por esse comando.

### `mkdirRemotePath` (`mutate.py:150`)

1. Especificação: `mkdir` cria primeiro no remoto e grava o índice. Se o caminho deve ficar pinado, cria `pinned/` (`docs/ARCHITECTURE.md`).
2. Vazio: caminho vazio levanta erro (`mutate.py:153`).
3. Identidade: um `itemPath`.
4. Outro processo: `cacheJobsLock` (`mutate.py:149`).
5. Falha a meio: se o índice falha depois do `mkdir`, a pasta já existe na nuvem. O refresh seguinte volta a vê-la.
6. Ciclo seguinte: a linha fica. Não há upload de conteúdo.
7. Quem chama: CLI `mkdir` (`cli.py:1021`) e o worker `mkdir`.

### `removeRemotePath` (`mutate.py:171`)

1. Especificação: Delete apaga a nuvem e o índice. Unpin não. Stat vazio com cópia local não apaga essa cópia (decisão 19).
2. Vazio: sem remoto e com cópia local, erro e a cópia fica (`mutate.py:180`). Sem remoto e sem cópia, sai só a linha (`mutate.py:183`).
3. Identidade: `--file` não apaga pasta e `--dir` não apaga ficheiro (`mutate.py:186`).
4. Outro processo: `cacheJobsLock` (`mutate.py:175`). `_pendingLocalEdit` vê `dirty`, `error` e mtime mais novo (`mutate.py:309`).
5. Falha a meio: se o `purge` ou o `deletefile` falha, o índice não é apagado. Se o índice sai e a cópia local não, `removePath` ignora o erro (`mutate.py:630`).
6. Ciclo seguinte: linha apagada não volta a enviar. Se a nuvem ainda tiver o item, o delta recria a linha.
7. Quem chama: CLI `rm` (`cli.py:1058`) e o worker `del`.

### `moveRemotePath` (`mutate.py:210`) e `_moveAcrossProfiles` (`mutate.py:263`)

1. Especificação: `moveto` no mesmo perfil, ou entre perfis com o remote de cada um. Destino sujo não é descartado. Sem `--overwrite`, destino ocupado é recusado.
2. Vazio: caminho vazio levanta erro (`mutate.py:219`). Origem igual ao destino no mesmo perfil volta (`mutate.py:221`).
3. Identidade: perfis diferentes entram em `_moveAcrossProfiles` (`mutate.py:225`). Cada URL activa o seu perfil em `_ownedRemote` (`mutate.py:200`).
4. Outro processo: `cacheJobsLock`. O índice da origem é apagado com o perfil da origem (`mutate.py:294`). O do destino é escrito com o perfil do destino (`mutate.py:300`).
5. Falha a meio: se o `moveto` falha, `_undoPreserve` devolve a cópia do destino (`mutate.py:245`, `mutate.py:289`). O `deleteIndexTree` da origem engole `FastOneDriveError` (`mutate.py:294`) e mesmo assim tira as cópias locais.
6. Ciclo seguinte: o conflito do destino, sem `itemId`, é enviado à parte. A origem some do índice; o delta trata o que o `moveto` já tirou da nuvem.
7. Quem chama: CLI `mv` (`cli.py:1081`). O worker `rename` passa os dois URL com o perfil de cada um (`kio_fastonedrive.cpp:1284`).

### `_preserveDirtyDestination` (`mutate.py:357`), `_finishPreserved` (`mutate.py:401`) e `_undoPreserve` (`mutate.py:419`)

1. Especificação: alteração local do destino não se perde num overwrite (decisão 22). O conflito não pode continuar com o `itemId` do ficheiro que ficou no caminho original.
2. Vazio: sem dirty ou sem cópia local devolve `""` (`mutate.py:336`).
3. Identidade: o nome leva a data (`mutate.py:370`). Sem linha no índice, `_indexShelvedCopy` cria a linha no nome novo (`mutate.py:435`).
4. Outro processo: o `itemId` só sai em `_finishPreserved`, depois da escrita na nuvem (`mutate.py:401`). Até lá, um refresh ainda casa o item.
5. Falha a meio: rename do índice falhado devolve os ficheiros e relança (`mutate.py:368`). `_undoPreserve` corre se o `moveto` ou o `copyto` falha.
6. Ciclo seguinte: `detachLocalIdentity` deixa baseline vazia e `dirty` só em ficheiros (`index.py:815`). Pasta não fica `dirty`. O sync envia o conflito como ficheiro novo. Um refresh não o apaga: baseline vazia ou `dirty` fica (`index.py:1992`).
7. Quem chama: `putLocalToRemote`, `moveRemotePath`, `_moveAcrossProfiles` e `copyDriveToDrive`.

### `_indexAfterWrite` (`mutate.py:557`), `_indexRemoteTree` (`mutate.py:520`) e `_adoptMirroredFiles` (`mutate.py:546`)

1. Especificação: depois do `put`, a linha recebe o stat remoto e a baseline do ficheiro já gravado. `pinned` segue `storedPinFlag`. Os ficheiros copiados para `pinned/` recebem essa baseline.
2. Vazio: stat ausente grava a linha sem token (`mutate.py:570`). Sem par caminho/stat, `_adoptMirroredFiles` não grava (`mutate.py:552`).
3. Identidade: um `itemPath`. O `itemId` vazio não apaga o que já está na linha. A adopção só corre para caminhos que o mirror copiou.
4. Outro processo: `upsertEntry` e `adoptDownloadedFile` fazem `commit`.
5. Falha a meio: o ficheiro já pode estar na nuvem e a linha ainda não. O refresh ou o próximo `put` regrava. Sem baseline, o ciclo descarrega o pin.
6. Ciclo seguinte: baseline igual ao conteúdo enviado não volta a enviar.
7. Quem chama: `putLocalToRemote`, `mkdirRemotePath`, `moveRemotePath` e `_moveAcrossProfiles`. `_adoptMirroredFiles` só o `put` de pasta.

### `_retireReplacedCaches` (`mutate.py:590`) e `_mirrorPinnedLocal` (`mutate.py:636`)

1. Especificação: o cache antigo do destino sai se não for a própria origem. Caminho pinado recebe a cópia nova em `pinned/`, inclusive os ficheiros de uma pasta. `skipPin` e nomes efémeros ficam de fora (`mutate.py:659`).
2. Vazio: sem cache, volta (`mutate.py:616`). Sem pin, `_mirrorPinnedLocal` devolve lista vazia (`mutate.py:637`).
3. Identidade: origem igual ao cache não é apagada (`mutate.py:619`). A mesma cópia pinada não faz `copy2` para ela mesma (`mutate.py:629`).
4. Outro processo: corre dentro do `cacheJobsLock`, depois de o conflito já ter saído do caminho.
5. Falha a meio: o envio e o índice da pasta já ocorreram. O ficheiro que não chegou a ser copiado fica sem pin; o ciclo descarrega.
6. Ciclo seguinte: `error` também sai (`mutate.py:610`). Cache limpo não volta a subir por cima do ficheiro enviado. O pin com baseline não é reenviado.
7. Quem chama: `putLocalToRemote`.

### `copyDriveToDrive` (`copy.py:40`) e `_dropReplacedLocal` (`copy.py:27`)

1. Especificação: cópia dentro do Drive é `rclone copyto`, não leitura pelo FUSE (decisão 6). Destino com alterações locais não é apagado.
2. Vazio: origem igual ao destino volta (`copy.py:48`). Caminho de item vazio não entra em `_dropReplacedLocal` (`copy.py:28`).
3. Identidade: `toRemotePath` activa o perfil de cada URL. O destino fica activo para o índice (`copy.py:50`).
4. Outro processo: `cacheJobsLock` durante a cópia e o refresh (`copy.py:54`).
5. Falha a meio: `_undoPreserve` antes de haver detach (`copy.py:69`). O refresh só corre depois de `_finishPreserved` (`copy.py:71`).
6. Ciclo seguinte: destino limpo perde a cópia local antiga e o refresh atualiza a linha. Destino sujo fica no conflito e não é apagado, porque `_dropReplacedLocal` volta se ainda está dirty (`copy.py:33`).
7. Quem chama: `_copySource` quando o destino é Drive (`cli.py:667`). O worker `copy` entre dois URL Fast-OneDrive usa o comando `copy`.

### `_copyRemoteToLocal` (`copy.py:85`) e `_copyDirectory` (`copy.py:133`)

1. Especificação: cópia para fora do Drive é `rclone`, com espaço livre verificado. Destino não pode ser o mount (decisão 6).
2. Vazio: pasta de destino vazia levanta erro (`copy.py:90`).
3. Identidade: o nome vem do remoto (`copy.py:105`). Pasta e ficheiro não caem um no outro (`copy.py:112`, `copy.py:127`).
4. Outro processo: não toma `cacheJobsLock`. Não altera o índice.
5. Falha a meio: o staging `.fod-staging-` sai (`copy.py:148`). O destino anterior fica se `swapIntoPlace` falha.
6. Ciclo seguinte: não há estado no índice. Repetir a cópia volta a descarregar.
7. Quem chama: `_copySource` (`cli.py:673`) e o worker `copy` para um URL `file`.

### `copy` (`kio_fastonedrive.cpp:828`), `put` (`kio_fastonedrive.cpp:1217`), `del` (`kio_fastonedrive.cpp:1285`), `rename` (`kio_fastonedrive.cpp:1306`) e `get` (`kio_fastonedrive.cpp:753`)

1. Especificação: o worker traduz para o CLI. Cada URL leva o seu perfil (`kio_fastonedrive.cpp:889`, `kio_fastonedrive.cpp:1338`). `get` com cópia local redirecciona; miniatura não hidrata (`kio_fastonedrive.cpp:788`).
2. Vazio: item vazio falha. `put` não chama o CLI se `write` ou `flush` falha (`kio_fastonedrive.cpp:1252`).
3. Identidade: `normalizedPath` lê o perfil do URL antes de guardar o id. Perfis diferentes limpam `--profile` para o CLI ver os dois (`kio_fastonedrive.cpp:881`, `kio_fastonedrive.cpp:1330`). `localPath` do `item` é o cache sujo quando ele existe (`search.py:164`).
4. Outro processo: o CLI é outro processo, com o `rclone.conf` do Fast-OneDrive.
5. Falha a meio: `put` deixa o temporário por apagar ao sair do `QTemporaryFile`. A cópia cancelada mata o processo (`kio_fastonedrive.cpp:1181`).
6. Ciclo seguinte: o estado fica no índice escrito pelo CLI, não no worker.
7. Quem chama: o Dolphin, via KIO.

### `rowToDict` (`search.py:153`) e `chmodItem` (`search.py:112`)

1. Especificação: a cópia e a abertura usam bytes já locais. Cache com alterações não é rebobinado (decisão 22). `chmod` grava `unixMode` e aplica o modo às cópias em `pinned/` e `cache/` (`docs/ARCHITECTURE.md`). Não marca dirty.
2. Vazio: sem ficheiro local, `localPath` fica vazio (`search.py:158`). Sem cópia, `chmod` só grava o índice (`search.py:116`).
3. Identidade: um caminho. Cache completo e sujo ganha ao pin (`search.py:164`). Cache limpo não ocupa `localPath`.
4. Outro processo: a leitura é o SQLite e o disco no momento da chamada. O modo fica no SQLite.
5. Falha a meio: `setUnixMode` já gravou se o `os.chmod` seguinte falha. O disco que não chegou a mudar mantém o modo antigo.
6. Ciclo seguinte: o cache sujo continua a ser a cópia que o sync envia. O modo gravado volta na listagem.
7. Quem chama: `item`/`children` (`search.py`), o worker `get`/`copy`, KRunner, Definições, CLI `chmod` e o FUSE `chmod`.

### `copyLocalPath` (`kio_fastonedrive.cpp:1009`)

1. Especificação: bytes já em `pinned/` ou `cache/` completo copiam-se no disco, sem ler o FUSE (`docs/ARCHITECTURE.md`).
2. Vazio: destino inexistente recebe o staging. Tipo diferente do destino existente falha sem apagar (`kio_fastonedrive.cpp:1023`).
3. Identidade: um par origem/destino.
4. Outro processo: `cp -a` ou `QFile::copy` para `.fod-staging-` ao lado do destino.
5. Falha a meio: o destino anterior está em `.fod-bak-` e volta se o staging não entra no lugar (`kio_fastonedrive.cpp:110`). O staging sai.
6. Ciclo seguinte: não há índice. O destino fica com o conteúdo novo, ou com o antigo se a troca falhou.
7. Quem chama: `copy` quando já há cópia local (`kio_fastonedrive.cpp:909`) ou depois do `hydrate` até ao limiar (`kio_fastonedrive.cpp:940`).

## Releitura desta passagem

Corrigidos e relidos: `rowToDict`, `chmodItem`, `putLocalToRemote`, `_indexRemoteTree`, `_adoptMirroredFiles`, `_clearPathDirty` e `_mirrorPinnedLocal`. Quem chama está nas linhas acima. `get` e `copy` passam a usar o cache completo sujo quando o pin ainda tem os bytes antigos. O `put` de uma pasta pinada grava esses ficheiros em `pinned/` e alinha a baseline com o stat remoto. `error` não sobrevive a um `put` que já substituiu o conteúdo. A mesma cópia pinada não volta a ser copiada para ela mesma. A releitura destas funções não acrescentou achado em aberto.

## URL, perfis, notify, mount

Funções que mudam perfil activo, disco, índice, remoto ou Locais, relidas depois da correcção: `toRemotePath`, `_normalizeRemotePath`, `_normalizeKioPath`, `_localToRemote`, `_prepareProfile`, `migrateLegacyIfNeeded`, `removeProfile`, `writeRemoteToken`, `removeRemoteSection`, `notifyDirChanged`, `_emitKdirNotify`, `setMountEnabled`, `applyProfileMount`, `enableProfileMount`, `disableProfileMount`, `_writeProfileUnit`, `activateProfile`, `replaceProfiles`, `_writeUserConfig`, `ensureUserConfig`, `upsertProfilePlace`, `removeProfilePlace`, `hideFuseFromDolphin`, `_rewriteFusePlaces`, `_writePlaces`.

`isRemotePath` e `_profileForRemoteText` não gravam. `isRemotePath` reconhece o remote de qualquer perfil (`paths.py:70`). `_profileForRemoteText` escolhe o nome mais longo (`paths.py:91`).

### `toRemotePath` (`paths.py:8`) e `_normalizeRemotePath` (`paths.py:98`)

1. Especificação: o CLI infere o perfil pelo URL ou pela pasta; `fastonedrive:/item/…` abre o perfil migrado (decisão 26). O remote escrito é o da conta desse perfil.
2. Vazio: caminho vazio levanta erro (`paths.py:11`).
3. Identidade: URL com `/p/<id>/` activa esse id (`paths.py:136`). Sem id, activa o perfil `legacy` mesmo que outro já esteja activo (`paths.py:139`). `remote:` activa o perfil desse remote (`paths.py:102`).
4. Outro processo: o perfil activo fica só neste processo. O remote vai na string devolvida.
5. Falha a meio: perfil desconhecido levanta erro antes de haver escrita.
6. Ciclo seguinte: cada chamada volta a activar o perfil do caminho.
7. Quem chama: `copyDriveToDrive` (`copy.py:46`), `_ownedRemote` (`mutate.py:206`) e os comandos do CLI.

### `_normalizeKioPath` (`paths.py:129`)

1. Especificação: `fastonedrive:/` e `fastonedrive:/item/…` são o perfil migrado (decisão 26).
2. Vazio: URL de busca, raiz ou `LEIA-ME.txt` levanta erro (`paths.py:148`).
3. Identidade: o id em `/p/<id>/` ganha ao perfil que já estava activo (`paths.py:135`).
4. Outro processo: só muda `_activeProfileId` deste processo.
5. Falha a meio: não escreve disco. O perfil activo pode já ter mudado se o erro é a seguir, no item.
6. Ciclo seguinte: a mesma URL activa o mesmo perfil.
7. Quem chama: `_normalizeRemotePath` (`paths.py:101`).

### `_prepareProfile` (`cli.py:424`)

1. Especificação: sem `--profile`, o CLI infere pelo URL ou pela pasta. `copy` e `mv` aceitam dois perfis (decisão 26).
2. Vazio: sem id inferido, `index` e `sync-pins` sem caminho não activam perfil (`cli.py:448`).
3. Identidade: dois perfis em `copy` ou `mv` voltam sem activar (`cli.py:439`), para cada URL activar o seu. `--profile` diferente do caminho levanta erro (`cli.py:443`).
4. Outro processo: o id activo não passa para outro processo.
5. Falha a meio: não escreve disco.
6. Ciclo seguinte: cada comando volta a inferir.
7. Quem chama: `run` (`cli.py:46`).

### `migrateLegacyIfNeeded` (`profiles.py:34`)

1. Especificação: a conta já existente passa ao perfil `onedrive` em `~/OneDrive` (decisão 26).
2. Vazio: sem chave `profiles` e sem instalação antiga, volta (`profiles.py:37`).
3. Identidade: o id é o remote se for um id válido (`profiles.py:46`).
4. Outro processo: `_MIGRATING` é só deste processo. O segundo espera `rcloneConfigLock` e, se o perfil já foi gravado, volta (`profiles.py:42`).
5. Falha a meio: o `move` que falha não grava `[[profiles]]`. O processo seguinte move o que ainda está na raiz; destino que já existe fica (`profiles.py:388`).
6. Ciclo seguinte: com `[[profiles]]`, não corre outra vez (`profiles.py:37`).
7. Quem chama: `ensureUserConfig` (`config.py:335`).

### `removeProfile` (`profiles.py:132`)

1. Especificação: remover apaga só dados locais (decisão 26). Alterações por enviar recusam a remoção.
2. Vazio: id desconhecido levanta erro (`profiles.py:136`). Sem base de dados, não há pendentes (`profiles.py:295`). Cópia local mais nova que `baseModTime`, ou sem baseline, recusa a remoção (`profiles.py:332`). Caminho vazio, absoluto ou com `..` não entra nessa comparação (`profiles.py:325`).
3. Identidade: sai o id pedido. O perfil activo, se for esse, é limpo (`profiles.py:160`).
4. Outro processo: o mount é parado antes do `rmtree` (`profiles.py:142`).
5. Falha a meio: o token só sai depois do `rmtree` (`profiles.py:154`). Se o `rmtree` falha, o token e a linha do perfil ficam. Se o `replaceProfiles` falha depois do token, a linha continua e a tentativa seguinte encontra a secção já ausente.
6. Ciclo seguinte: id que já saiu levanta «perfil desconhecido».
7. Quem chama: definições `_removeProfile` (`settings.py:321`).

### `writeRemoteToken` (`profiles.py:164`) e `removeRemoteSection` (`profiles.py:229`)

1. Especificação: `rclone authorize` grava o token sob `flock`; a secção é o remote do perfil (decisão 26).
2. Vazio: nome vazio ou token com quebra de linha levantam erro (`profiles.py:166`). Secção ausente no delete não é erro (`profiles.py:251`).
3. Identidade: a secção é `[remote]`. Outras secções ficam (`profiles.py:190`).
4. Outro processo: `rcloneConfigLock` (`profiles.py:171`, `profiles.py:238`).
5. Falha a meio: o temporário do `rclone.conf` sai se o `replace` falha (`profiles.py:285`). O delete do rclone, se falha por outro motivo, apaga a secção no texto (`profiles.py:253`).
6. Ciclo seguinte: o mesmo token volta a ser escrito na mesma secção.
7. Quem chama: o assistente de login. `removeProfile` chama `removeRemoteSection` (`profiles.py:154`).

### `notifyDirChanged` (`notify.py:76`) e `_emitKdirNotify` (`notify.py:120`)

1. Especificação: o Dolphin refresca com KDirNotify (decisão 19). O perfil migrado também recebe o URL antigo `fastonedrive:`.
2. Vazio: sem URLs, volta (`notify.py:121`). Sem `qdbus` nem `dbus-send`, volta (`notify.py:135`).
3. Identidade: a raiz é `fastonedrive:/p/<id>` do perfil activo (`notify.py:117`). Perfil `legacy` também notifica `fastonedrive:` (`notify.py:81`).
4. Outro processo: o sinal é o que o Dolphin lê. Não há estado deste processo para o outro CLI.
5. Falha a meio: o processo do `qdbus` falhado não levanta erro (`notify.py:132`).
6. Ciclo seguinte: outra alteração volta a notificar.
7. Quem chama: mutate, hydrate, sync, index e search, depois de gravar o índice.

### `setMountEnabled` (`mount.py:65`)

1. Especificação: cada perfil tem o seu FUSE; `Conflicts=` com o mount antigo só em `~/OneDrive` (decisão 8).
2. Vazio: sem perfis, levanta erro (`mount.py:68`).
3. Identidade: sem perfil activo, a flag muda em todos (`mount.py:70`). Com perfil activo, só nesse.
4. Outro processo: a unidade systemd é que o próximo boot lê. A flag fica no `config.toml`.
5. Falha a meio: se `enable --now` não deixa a unidade activa, `mountEnabled` volta ao valor anterior (`mount.py:78`). Se a unidade chegou a arrancar, a flag mantém-se.
6. Ciclo seguinte: `applyProfileMount` lê a flag gravada (`mount.py:89`).
7. Quem chama: CLI `mount` (`cli.py:1139`).

### `enableProfileMount` (`mount.py:96`) e `_writeProfileUnit` (`mount.py:149`)

1. Especificação: `fast-onedrive-mount@<id>.service` corre `fuse --profile <id>` (decisão 8). `--profile` é argumento global do CLI (`cli.py:69`).
2. Vazio: id vazio em `fuseCommand` não mete `--profile` (`mount.py:137`).
3. Identidade: a unidade e a pasta são as do perfil (`mount.py:150`).
4. Outro processo: o systemd user lê o ficheiro da unidade.
5. Falha a meio: `daemon-reload` ou `enable` falhado levanta erro (`mount.py:199`). A unidade é escrita num temporário e só depois substitui (`mount.py:182`). Se essa troca falha, a unidade anterior fica. A flag fica ao cuidado de `setMountEnabled`.
6. Ciclo seguinte: voltar a ligar reescreve a unidade e torna a activar.
7. Quem chama: `applyProfileMount` (`mount.py:93`) e a migração (`profiles.py:64`).

### `disableProfileMount` (`mount.py:105`)

1. Especificação: desligar o FUSE do perfil não apaga a nuvem.
2. Vazio: `disable --now` com a unidade já ausente não levanta erro (`mount.py:107`).
3. Identidade: a unidade é `fast-onedrive-mount@<id>` (`mount.py:106`).
4. Outro processo: `systemctl` e `fusermount3` mexem no mount deste utilizador.
5. Falha a meio: o `fusermount` falhado não repõe a unidade. O `disable` já foi pedido.
6. Ciclo seguinte: a unidade desactivada não volta sozinha.
7. Quem chama: `applyProfileMount` e `removeProfile` (`profiles.py:142`).

### `activateProfile` (`config.py:135`) e `replaceProfiles` (`config.py:407`)

1. Especificação: o id escolhe remote, mount, índice e pins (decisão 26). Renomear não muda o id.
2. Vazio: id desconhecido levanta erro (`config.py:138`). Lista vazia grava `profiles = []` (`config.py:750`).
3. Identidade: `_activeProfileId` é o id pedido. `replaceProfile` troca só o id igual (`config.py:430`).
4. Outro processo: o id activo não sai deste processo. O toml é que o outro lê.
5. Falha a meio: o toml novo está num temporário; o antigo fica se o `replace` falha (`config.py:748`).
6. Ciclo seguinte: `listProfiles` lê o ficheiro substituído.
7. Quem chama: CLI, paths, mount, definições e KRunner. `replaceProfiles` é chamado pela migração e por `removeProfile`.

### `upsertProfilePlace` (`kioinstall.py:270`) e `_rewriteFusePlaces` (`kioinstall.py:338`)

1. Especificação: cada perfil é um Local `fastonedrive:/p/<id>/` (decisão 26). O FUSE não aparece nos Locais.
2. Vazio: sem `user-places.xbel`, o upsert levanta erro (`kioinstall.py:275`). Sem o ficheiro, `hideFuseFromDolphin` não reescreve bookmarks (`kioinstall.py:266`).
3. Identidade: o bookmark do perfil migrado com href `fastonedrive:/` passa a `/p/<id>/` (`kioinstall.py:289`). Entre mounts, ganha o que tem mais componentes (`kioinstall.py:377`).
4. Outro processo: o Dolphin lê o xbel. Não há lock com o Dolphin.
5. Falha a meio: o xbel novo está num temporário (`kioinstall.py:560`). O antigo fica se o `replace` falha.
6. Ciclo seguinte: o mesmo perfil actualiza o bookmark que já tem o id, em vez de criar outro (`kioinstall.py:289`).
7. Quem chama: migração, `enableProfileMount` (`mount.py:102`) e a instalação do KIO.

### `removeProfilePlace` (`kioinstall.py:298`)

1. Especificação: remover o perfil tira o Local deste PC.
2. Vazio: sem xbel, volta (`kioinstall.py:301`).
3. Identidade: sai o href `/p/<id>/` ou o id `FastOneDrivePlace-<id>` (`kioinstall.py:314`).
4. Outro processo: o Dolphin lê o ficheiro depois.
5. Falha a meio: sem `commit` próprio; o `replace` é atómico. Se falha, o bookmark continua.
6. Ciclo seguinte: bookmark já ausente não muda o ficheiro (`kioinstall.py:317`).
7. Quem chama: `removeProfile` (`profiles.py:144`).

### `hideFuseFromDolphin` (`kioinstall.py:258`)

1. Especificação: o FUSE de cada perfil não aparece nos Locais, nos Dispositivos nem na Home (`docs/ARCHITECTURE.md`).
2. Vazio: sem perfis esconde `~/OneDrive` (`kioinstall.py:261`). Sem `user-places.xbel`, não reescreve bookmarks (`kioinstall.py:266`).
3. Identidade: cada `mountPath` de perfil (`kioinstall.py:264`).
4. Outro processo: o Dolphin lê `.hidden` e o xbel. Não há lock.
5. Falha a meio: `.hidden` só é trocado no fim (`kioinstall.py:336`). Se essa troca falha, o xbel não chega a ser reescrito.
6. Ciclo seguinte: o nome que já está em `.hidden` não é repetido (`kioinstall.py:333`).
7. Quem chama: `installKio` (`kioinstall.py:67`), `applyProfileMount` (`mount.py:91`) e `enableProfileMount` (`mount.py:102`).

## Releitura desta passagem

Corrigidos e relidos: `_writeProfileUnit`, `_profileHasPending`, `_rowHasPending`, `removeProfile`, `_prepareProfile`, `toRemotePath`, `notifyDirChanged` e `setMountEnabled`. Quem chama está nas linhas acima. A unidade do FUSE deixa de ser truncada no sítio. Remover um perfil com cópia local por enviar passa a ser recusado, mesmo quando `syncState` ainda está vazio. `copy` e `mv` com dois perfis continuam sem activar um perfil único. A releitura não acrescentou achado em aberto.


## Login de perfil

Funções que mudam disco, remote ou o perfil, relidas depois da correcção: `OauthSession.cancel`, `advance`, `_prepareRemote`, `_storeAuthorizedToken`, `_authorizeOnedrive`, `_call`, `_createProfile`, `_rollbackNewProfile`.

`_resultIsToken` não grava. Recusa um token no `--result` (`oauthsetup.py:306`).

### `advance` (`oauthsetup.py:62`) e `_storeAuthorizedToken` (`oauthsetup.py:121`)

1. Especificação: `rclone authorize` grava o token sob flock; `config update --non-interactive` segue para `drive_id` e `drive_type` sem o token em argv (decisão 26).
2. Vazio: resposta vazia numa pergunta pendente segue para o rclone (`oauthsetup.py:71`). Sem pergunta pendente, levanta erro (`oauthsetup.py:68`).
3. Identidade: a secção é o id do perfil novo (`oauthsetup.py:140`).
4. Outro processo: `rcloneConfigLock` durante o `config update` (`oauthsetup.py:220`). O `authorize` pode ser morto por `cancel` (`oauthsetup.py:50`).
5. Falha a meio: o token pode já estar na secção. Se faltam `drive_id` ou `drive_type`, a tentativa seguinte apaga a secção (`oauthsetup.py:83`). Se a secção está completa e ainda não há perfil, a tentativa volta a entrar (`oauthsetup.py:80`).
6. Ciclo seguinte: um perfil que já existe com esse remote é recusado (`oauthsetup.py:82`).
7. Quem chama: o assistente `_runAdvance` (`wizard.py:195`).

### `cancel` (`oauthsetup.py:50`)

1. Especificação: fechar a janela não deixa o login continuar.
2. Vazio: sem processo, marca o cancelamento e volta (`oauthsetup.py:51`).
3. Identidade: mata o processo que está registado, authorize ou config update (`oauthsetup.py:164`).
4. Outro processo: o kill é o deste `Popen`. Outro CLI não o vê.
5. Falha a meio: se o `wait` excede, o processo já foi morto (`oauthsetup.py:58`).
6. Ciclo seguinte: `advance` vê o cancelamento e levanta erro (`oauthsetup.py:63`).
7. Quem chama: `reject` do assistente (`wizard.py:116`).

### `_createProfile` (`wizard.py:264`) e `_rollbackNewProfile` (`wizard.py:291`)

1. Especificação: perfil novo tem Local `/p/<id>/`, FUSE próprio e remote com o mesmo id (decisão 26).
2. Vazio: nome vazio nem chega aqui (`wizard.py:141`).
3. Identidade: o id foi escolhido antes do login e não muda (`wizard.py:162`).
4. Outro processo: o mount e o Local ficam em ficheiros. O índice arranca noutro processo (`wizard.py:286`).
5. Falha a meio: qualquer erro desfaz o perfil, se já entrou na config, ou a secção do remote (`wizard.py:288`). O erro original chega à janela (`wizard.py:223`).
6. Ciclo seguinte: id desfeito pode ser pedido outra vez. Secção órfã não bloqueia (`oauthsetup.py:80`).
7. Quem chama: `_onLoginFinished` quando o rclone termina sem pergunta (`wizard.py:222`). Cancelar chama o desfazer e não cria o perfil (`wizard.py:210`).

## Releitura desta passagem

Corrigidos e relidos: `cancel`, `_authorizeOnedrive`, `_prepareRemote`, `_createProfile`, `_rollbackNewProfile`. Fechar a janela mata o `authorize` e não grava o perfil. Uma falha a criar o Local ou o mount tira o perfil e o token que essa tentativa gravou. Uma secção já completa, sem perfil, não impede a tentativa seguinte. A releitura não acrescentou achado em aberto.

## Definições

Funções que mudam perfil activo, disco, índice ou remoto, relidas depois da correcção: `closeEvent`, `_reloadProfileCombo`, `_onProfileChanged`, `_addProfile`, `_restoreActiveProfile`, `_renameProfile`, `_removeProfile`, `_refreshQuota`, `_onQuotaFinished`, `_startIndex`, `_persist`, `_importSystemRclone`, `_refreshRemotes`, `_testRclone`, `_openSelected`, `_onOpenFinished`, `dropClient`, `importSystemRcloneConfig`, `remoteAbout`.

`_clearSearch`, `_refreshFastStatus`, `_showProfileRemote` e `_setOpenBusy` não gravam disco. Mudam a lista, os caminhos mostrados, o combo ou a barra.

### `closeEvent` (`settings.py:204`)

1. Especificação: com bandeja, Fechar esconde; Sair no ícone termina (decisão 20).
2. Vazio: sem bandeja, `hideOnClose` é falso e `quit` corre (`settings.py:212`).
3. Identidade: a instância é a do socket `fastonedrive-ui`.
4. Outro processo: o socket fica neste processo até ao `quit`.
5. Falha a meio: esconder não apaga a config.
6. Ciclo seguinte: outra invocação pede `show` à instância que ficou.
7. Quem chama: o botão Fechar e o X.

### `_onProfileChanged` (`settings.py:265`) e `_reloadProfileCombo` (`settings.py:247`)

1. Especificação: o perfil escolhido passa a ser o activo; a pesquisa é o índice desse perfil (decisão 26).
2. Vazio: id vazio volta (`settings.py:270`).
3. Identidade: a lista de resultados sai (`settings.py:274`). Durante um download a barra não muda de perfil (`settings.py:266`).
4. Outro processo: `_activeProfileId` é só deste processo. O toml não muda aqui.
5. Falha a meio: `activateProfile` ou corre ou levanta antes de limpar a pesquisa.
6. Ciclo seguinte: `_loadValues` lê o toml do perfil activo (`settings.py:1029`).
7. Quem chama: o combo. `_reloadProfileCombo` também corre no arranque, ao renomear e ao remover.

### `_addProfile` (`settings.py:278`) e `_restoreActiveProfile` (`settings.py:292`)

1. Especificação: perfil novo tem Local e FUSE próprios (decisão 26).
2. Vazio: o assistente recusa nome vazio antes de criar.
3. Identidade: se o assistente não cria, o id que estava seleccionado volta (`settings.py:284`).
4. Outro processo: o assistente grava o `rclone.conf` sob flock. Esta janela só reactiva o id.
5. Falha a meio: o rollback do assistente pode limpar o perfil activo; `_restoreActiveProfile` repõe o id se ele ainda existe.
6. Ciclo seguinte: perfil criado fica seleccionado e a pesquisa anterior sai (`settings.py:288`).
7. Quem chama: o botão Novo perfil.

### `_renameProfile` (`settings.py:302`)

1. Especificação: renomear muda o nome visível e o título do Local; o id e a pasta ficam (decisão 26).
2. Vazio: nome vazio avisa e volta (`settings.py:316`).
3. Identidade: o id copiado é o do perfil activo (`settings.py:322`).
4. Outro processo: o Dolphin lê o `user-places.xbel` depois do `upsert`.
5. Falha a meio: se o título falha, a config não muda (`settings.py:333`). Se a config falha depois do título, o mesmo nome novo volta a gravar os dois.
6. Ciclo seguinte: o combo relê o nome gravado (`settings.py:337`).
7. Quem chama: o botão Renomear.

### `_removeProfile` (`settings.py:339`)

1. Especificação: remover apaga só dados locais (decisão 26).
2. Vazio: sem perfil activo volta (`settings.py:341`).
3. Identidade: sai o id que está activo (`settings.py:355`).
4. Outro processo: `removeProfile` pára o mount e apaga a pasta do perfil.
5. Falha a meio: o erro fica na janela e a pesquisa não é limpa (`settings.py:356`).
6. Ciclo seguinte: o combo passa ao perfil que ficou e a pesquisa sai (`settings.py:359`).
7. Quem chama: o botão Remover.

### `_refreshQuota` (`settings.py:928`) e `remoteAbout` (`rclone.py:464`)

1. Especificação: a quota é `rclone about` da conta do perfil activo.
2. Vazio: remote vazio usa `remoteRoot()` (`rclone.py:466`).
3. Identidade: o id do perfil vai no resultado (`settings.py:954`). Se já não é o perfil activo, o número não é mostrado (`settings.py:961`).
4. Outro processo: o `about` é outro processo rclone, com o remote já fixado na thread.
5. Falha a meio: o texto de erro fica na etiqueta quando o id ainda coincide.
6. Ciclo seguinte: dentro de 60 s e com o mesmo perfil, reutiliza o último payload (`settings.py:929`).
7. Quem chama: `_loadValues` e o `showEvent`.

### `_startIndex` (`settings.py:992`)

1. Especificação: Indexar agora corre o índice do perfil activo em segundo plano.
2. Vazio: se já corre, só refresca (`settings.py:994`).
3. Identidade: `startBackgroundIndex` mete `--profile` do perfil activo (`index.py:260`).
4. Outro processo: o índice é outro processo, com o lock `index.lock` desse perfil.
5. Falha a meio: se o processo não arranca, a janela avisa (`settings.py:998`).
6. Ciclo seguinte: o timer lê `index-status.json` desse perfil.
7. Quem chama: o botão Indexar agora.

### `_persist` (`settings.py:1072`)

1. Especificação: os separadores gravam `config.toml`; o mount do perfil só muda ao Guardar (decisão 17 e 26). `openMaxBytes`, `skipPin` e excludes são globais.
2. Vazio: remote vazio no combo, com perfil activo, não substitui o `remoteName` global (`settings.py:1082`).
3. Identidade: a flag de mount escrita no perfil é a do perfil activo (`settings.py:1112`). O combo volta ao `remoteName` desse perfil (`settings.py:1104`).
4. Outro processo: o toml é substituído por temporário. A unidade systemd é o outro processo do FUSE.
5. Falha a meio: se `enable --now` não deixa a unidade activa, `mountEnabled` e a caixa voltam ao valor anterior (`settings.py:1119`). Testar e Importar não mudam a flag (`settings.py:1080`).
6. Ciclo seguinte: `_loadValues` lê a flag do perfil, não a chave global.
7. Quem chama: Guardar (`applyFuse`), Testar, Importar e Actualizar remotes.

### `importSystemRcloneConfig` (`config.py:360`)

1. Especificação: a cópia do `rclone.conf` do sistema não altera o ficheiro do mount antigo. O token continua sob flock (decisão 26).
2. Vazio: sem ficheiro de origem, levanta erro (`config.py:363`). Origem igual ao destino volta sem copiar (`config.py:366`).
3. Identidade: o destino é o `rcloneConfig` gravado.
4. Outro processo: a substituição corre dentro de `rcloneConfigLock` (`config.py:369`).
5. Falha a meio: o temporário sai e o `rclone.conf` anterior fica (`config.py:378`).
6. Ciclo seguinte: a mesma importação volta a copiar o ficheiro do sistema.
7. Quem chama: `_importSystemRclone` (`settings.py:1167`), depois de `_persist`.

### `_openSelected` (`settings.py:1247`)

1. Especificação: pasta abre no Local; até `openMaxBytes`, hidrata `cache/`; acima, abre o FUSE do perfil se estiver montado (decisão 24).
2. Vazio: sem selecção volta (`settings.py:1251`).
3. Identidade: o remote é resolvido antes da thread (`settings.py:1276`). O id activo é reposto no fim (`settings.py:1290`).
4. Outro processo: `hydrateRemote` é rclone noutro processo. A barra de perfil fica desligada enquanto corre (`settings.py:1309`).
5. Falha a meio: o erro chega à janela e a barra volta a ligar (`settings.py:1315`).
6. Ciclo seguinte: a pesquisa continua a ser a do perfil reposto.
7. Quem chama: Abrir e o duplo clique.

### `dropClient` (`settings.py:1392`)

1. Especificação: segunda instância pede à primeira para mostrar a janela (decisão 20).
2. Vazio: cliente já fora da lista volta (`settings.py:1393`).
3. Identidade: cada ligação é um socket.
4. Outro processo: quem liga é o outro `settings`.
5. Falha a meio: `show` tanto pode chegar em `readyRead` como ao fechar (`settings.py:1407`).
6. Ciclo seguinte: outra invocação liga de novo.
7. Quem chama: `onNewConnection`.

## Releitura desta passagem

Corrigidos e relidos: `_onProfileChanged`, `_addProfile`, `_restoreActiveProfile`, `_renameProfile`, `_refreshQuota`, `_onQuotaFinished`, `_persist`, `_openSelected`, `_onOpenFinished`, `closeEvent`, `dropClient`, `importSystemRcloneConfig`, `remoteAbout`, `storedRemoteName`. Mudar de perfil limpa a pesquisa. A quota de uma conta não fica na outra. Um assistente falhado não deixa a janela no perfil errado. O mount não fica marcado se a unidade não arrancou. O título do Local é gravado antes do nome na config. Importar não trunca o `rclone.conf`. Sem bandeja, Fechar termina o processo. A releitura não acrescentou achado em aberto.

## KRunner

Funções que mudam perfil activo, disco ou remoto: `matchQuery`, `runMatch`, `openIndexedPath`, `installRunner`.

`_splitMatchId` não grava. Parte o id em perfil e caminho (`krunner.py:186`).

### `matchQuery` (`krunner.py:71`)

1. Especificação: a query lê o SQLite de cada perfil, sem FUSE (decisão 11).
2. Vazio: menos de 3 letras devolve lista vazia (`krunner.py:73`).
3. Identidade: o id do resultado é `perfil`, tab, caminho (`krunner.py:215`).
4. Outro processo: activa cada perfil só nesta processo, para abrir a base dele (`krunner.py:79`).
5. Falha a meio: um perfil que levanta erro é saltado (`krunner.py:81`). Os outros entram na lista.
6. Ciclo seguinte: `Run` não usa o perfil que ficou activo; usa o id do resultado.
7. Quem chama: `Match` do D-Bus (`krunner.py:42`).

### `runMatch` (`krunner.py:89`) e `openIndexedPath` (`krunner.py:112`)

1. Especificação: mantido abre `pinned/`. Até `openMaxBytes`, hidrata. Acima, abre o FUSE desse perfil (decisão 24). Copiar pede a pasta com kdialog (decisão 17).
2. Vazio: id sem perfil e sem caminho volta (`krunner.py:93`).
3. Identidade: o perfil do id é activado antes do remote (`krunner.py:96`).
4. Outro processo: copiar e pin são outro processo, com o remote já na argv (`krunner.py:101`).
5. Falha a meio: abrir que falha mostra notificação (`krunner.py:108`). O hydrate que falha a meio fica no `cache/` como o próprio hydrate define.
6. Ciclo seguinte: outro `Run` volta a activar o perfil do id.
7. Quem chama: `Run` do D-Bus (`krunner.py:53`).

### `installRunner` (`krunner.py:144`)

1. Especificação: instala o plugin D-Bus do KRunner.
2. Vazio: sem o binário ou sem o template, levanta erro (`krunner.py:147`, `krunner.py:156`).
3. Identidade: o serviço é `org.fastonedrive.krunner`.
4. Outro processo: o KRunner lê o `.desktop` e o serviço D-Bus depois do `kbuildsycoca6`.
5. Falha a meio: cada ficheiro é substituído por `writeInstalledText` (`krunner.py:159`). O anterior fica se o temporário falha.
6. Ciclo seguinte: instalar outra vez reescreve os três ficheiros.
7. Quem chama: o CLI `install-runner`.

## Releitura desta passagem

Corrigido e relido: `installRunner`, que passa a usar `writeInstalledText`. O serviço D-Bus e o plugin deixam de ser truncados no sítio. `matchQuery` e `openIndexedPath` não foram alterados. A releitura não acrescentou achado em aberto.

## Menus Dolphin

Funções que mudam disco: `writeInstalledText`, `installCliWrapper`, `installServiceMenuHelper`, `installSettingsDesktop`, `installAutostartDesktop`, `installSchemeHandler`, `installMenus`. `dolphin/servicemenu.py`: `actionArgs`, `spawnCli`, `main`.

`pickDestDir` não grava. Abre o kdialog na pasta de cópia (`menus.py:106`).

### `writeInstalledText` (`menus.py:36`)

1. Especificação: `install-menus` e `install-runner` gravam atalhos que o Plasma lê.
2. Vazio: o corpo é o template já lido pelo chamador.
3. Identidade: o temporário leva o pid (`menus.py:38`), para duas instalações não partilharem o mesmo ficheiro.
4. Outro processo: `os.replace` troca o inode. Quem já abriu o ficheiro antigo continua com ele.
5. Falha a meio: o temporário sai e o destino anterior fica (`menus.py:44`).
6. Ciclo seguinte: instalar outra vez reescreve o mesmo caminho.
7. Quem chama: os `install*` de `menus.py` e `installRunner`.

### `installMenus` (`menus.py:181`)

1. Especificação: o menu tem manter, deixar de manter, partilhar e libertar espaço, só em `fastonedrive://`. O autostart é `settings --tray` (decisão 19 e 20).
2. Vazio: sem template, levanta erro antes de escrever o menu (`menus.py:187`).
3. Identidade: o `Exec` aponta ao wrapper `~/.local/bin/fastonedrive` e ao helper, com o launcher entre aspas no template.
4. Outro processo: o Dolphin lê o `.desktop` depois do `kbuildsycoca6` (`menus.py:202`).
5. Falha a meio: o wrapper, o helper e cada `.desktop` já substituídos ficam inteiros. O que ainda não correu conserva o ficheiro anterior.
6. Ciclo seguinte: a mesma instalação reescreve todos.
7. Quem chama: o CLI `install-menus`.

### `actionArgs` (`dolphin/servicemenu.py:25`) e `spawnCli` (`dolphin/servicemenu.py:44`)

1. Especificação: o menu não passa `--profile`; o CLI infere pelo URL (decisão 26). Não é unpin quando a acção é apagar: apagar não está neste menu.
2. Vazio: acção desconhecida sai 1 e fica no log (`servicemenu.py:38`). Sem wrapper, sai 1 (`servicemenu.py:46`).
3. Identidade: cada URL segue na argv, sem shell (`servicemenu.py:51`).
4. Outro processo: o CLI é outro processo. O stderr desse processo não volta ao Dolphin (`servicemenu.py:56`). O `--notify` é que mostra o erro das acções que o tratam.
5. Falha a meio: o `Popen` não espera. O estado fica no CLI.
6. Ciclo seguinte: outra acção do menu dispara outro CLI.
7. Quem chama: `main` (`servicemenu.py:67`), a partir do `Exec` do `.desktop`.

## Releitura desta passagem

Corrigidos e relidos: `writeInstalledText` e quem o chama em `installMenus` e `installRunner`. O menu, o wrapper, o helper e os `.desktop` deixam de ser truncados no sítio. Duas instalações não partilham o temporário. A releitura não acrescentou achado em aberto.

## Instalação do worker

Funções que mudam disco, relidas depois da correcção: `installKio`, `_installPluginEnv`, `_systemdPluginPath`, `_buildAndInstallWorker`, `_buildWithCmake`, `_buildWithMake`, `_runCmake`, `_installPlace`, `_insertPlaceBookmark`, `hideFuseFromDolphin`, `_hideMountInHome`. O alvo `install` do `kio/Makefile` também.

`_findPlugin`, `_qtPluginRoot` e `_pluginEnv` não gravam. `_quoteEnvValue`, `_unquoteEnvValue` e `_earlierQtPluginPath` só calculam o texto do `environment.d`.

### `installKio` (`kioinstall.py:60`)

1. Especificação: `install-kio` instala o worker em `~/.local` (`docs/PACKAGING.md`). Cada perfil fica um Local e o FUSE sai dos Locais.
2. Vazio: sem fonte do worker, levanta erro (`kioinstall.py:158`). Sem cmake e sem make, levanta erro (`kioinstall.py:167`).
3. Identidade: o plugin é `kio_fastonedrive.so` sob o prefixo do utilizador.
4. Outro processo: o Dolphin passa a ler o `.so`, o script de sessão e o xbel. O `sycoca` é reconstruído no fim (`kioinstall.py:68`).
5. Falha a meio: o wrapper e o scheme handler podem já estar gravados. O `.so` anterior fica se a cópia não chega ao `mv`. O xbel e o `.hidden` anteriores ficam se a troca falha.
6. Ciclo seguinte: instalar outra vez recompila e reescreve os mesmos ficheiros.
7. Quem chama: CLI `install-kio` (`cli.py:565`) e `_ensureIntegrations` (`wizard.py:336`).

### `_installPluginEnv` (`kioinstall.py:136`) e `_systemdPluginPath` (`kioinstall.py:109`)

1. Especificação: o Dolphin tem de encontrar o plugin em `~/.local/lib/qt6/plugins`.
2. Vazio: sem `environment.d`, o valor é só o directório do plugin (`kioinstall.py:90`).
3. Identidade: o script é `fastonedrive.sh` e o conf é `90-fastonedrive.conf`.
4. Outro processo: a sessão Plasma lê o script; o systemd do utilizador lê o conf. Os `.conf` com nome anterior a `90-` entram no valor (`kioinstall.py:93`). O próprio `90-` não é relido, para não duplicar o directório.
5. Falha a meio: `writeInstalledText` deixa o ficheiro anterior se o temporário falha (`menus.py:43`).
6. Ciclo seguinte: instalar outra vez reescreve os dois, com o directório do plugin à frente.
7. Quem chama: `installKio` (`kioinstall.py:65`).

### `_buildAndInstallWorker` (`kioinstall.py:155`), `_buildWithMake` (`kioinstall.py:194`) e `_buildWithCmake` (`kioinstall.py:176`)

1. Especificação: o desenvolvimento compila o worker para `~/.local` (`docs/PACKAGING.md`).
2. Vazio: sem `CMakeLists.txt` e sem `Makefile`, levanta erro (`kioinstall.py:158`).
3. Identidade: com `Makefile` e `make` no `PATH`, usa make (`kioinstall.py:162`). Sem `make`, usa cmake (`kioinstall.py:164`).
4. Outro processo: o `mv` troca o nome do `.so` (`kio/Makefile:32`). Quem já mapeou o inode antigo continua com ele.
5. Falha a meio: se o `cp` falha, o make pára e o `.so` anterior fica. O cmake tem timeout de 120 s por invocação (`kioinstall.py:231`); o directório `kio/build` fica para a tentativa seguinte.
6. Ciclo seguinte: make ou cmake voltam a instalar por cima.
7. Quem chama: `installKio` (`kioinstall.py:64`).

### `_installPlace` (`kioinstall.py:249`) e `_insertPlaceBookmark` (`kioinstall.py:462`)

1. Especificação: sem perfis, o Local é `fastonedrive:/` (`docs/DECISIONS.md` decisão 26). Com perfis, um Local por perfil.
2. Vazio: sem xbel, levanta erro (`kioinstall.py:465`).
3. Identidade: o bookmark legado usa o href e o id fixos (`kioinstall.py:252`). Se o href já está no texto, não insere (`kioinstall.py:467`).
4. Outro processo: o Dolphin lê o xbel depois do `replace`.
5. Falha a meio: a troca é no fim (`kioinstall.py:487`). O xbel anterior fica se o temporário falha.
6. Ciclo seguinte: href já presente não cria outro bookmark.
7. Quem chama: `installKio` (`kioinstall.py:66`). `_insertPlaceBookmark` só corre sem perfis.

### `_hideMountInHome` (`kioinstall.py:321`)

1. Especificação: a pasta do FUSE directamente na Home não aparece no Dolphin.
2. Vazio: mount que não é filho da Home volta (`kioinstall.py:325`). `.hidden` ausente é criado na troca.
3. Identidade: uma linha por nome de pasta (`kioinstall.py:326`).
4. Outro processo: o Dolphin lê `.hidden`. Dois processos podem escrever por cima um do outro; a chamada seguinte acrescenta o nome que faltou.
5. Falha a meio: o ficheiro anterior fica (`kioinstall.py:336`).
6. Ciclo seguinte: nome já presente volta sem escrever (`kioinstall.py:333`).
7. Quem chama: `hideFuseFromDolphin` (`kioinstall.py:261` e `kioinstall.py:264`).

## Releitura desta passagem

Corrigidos e relidos: `_installPluginEnv`, `_systemdPluginPath`, `_buildAndInstallWorker`, `_buildWithMake`, `_hideMountInHome`, `_insertPlaceBookmark` e o alvo `install` do Makefile. Quem chama está nas linhas acima. O script de sessão, o `environment.d`, o `.hidden` e o xbel sem perfis deixam de ser truncados no sítio. O `.so` deixa de ser truncado no inode que o Dolphin tem aberto. Sem `make`, a instalação usa cmake. A releitura tirou a descodificação de escapes em `_unquoteEnvValue`, que alterava um valor citado com barras invertidas; fica só o par de aspas exterior. Não ficou achado em aberto.

## Busca

Funções que mudam o processo ou o que o Dolphin lista: `search`, `_cmdSearch`, `_endDashPositionals`, `_endSearchQuery`, `_endRawPath`, `runDolphinSearch`. No worker, `searchQuery` e o ramo `/search/` de `listDir`.

`search` não grava índice nem disco. `rowToDict` também não.

### `search` (`search.py:33`) e `_cmdSearch` (`cli.py:523`)

1. Especificação: a busca é nome e caminho no SQLite, sem FUSE (`docs/DECISIONS.md` decisão 5 e 11).
2. Vazio: termo vazio levanta erro (`search.py:36`). Limite `0` não mete `LIMIT` (`search.py:48`).
3. Identidade: o padrão é `LIKE` com escape de `%`, `_` e `\` (`search.py:314`). O perfil é o que `_prepareProfile` activou.
4. Outro processo: lê o SQLite do perfil. Não escreve.
5. Falha a meio: a query não altera o índice.
6. Ciclo seguinte: a mesma busca lê o índice actual.
7. Quem chama: CLI `search` e o worker `listDir` em `/search/` (`kio/kio_fastonedrive.cpp:578`), com `--profile` quando o URL tem `/p/<id>/`.

### `_endDashPositionals` (`cli.py:366`)

1. Especificação: o termo da busca e o caminho de `item` e `children` são posicionais.
2. Vazio: sem subcomando, devolve o argv (`cli.py:379`).
3. Identidade: `--profile` anterior ao comando não é o comando (`cli.py:372`). `--limit` e `--limit=` continuam opção da busca (`cli.py:398`).
4. Outro processo: só muda o argv deste processo, antes do `parse`.
5. Falha a meio: não escreve disco.
6. Ciclo seguinte: cada comando volta a passar pelo mesmo filtro (`cli.py:39`).
7. Quem chama: `run` (`cli.py:39`).

### `runDolphinSearch` (`kioinstall.py:22`)

1. Especificação: `dolphin-search` abre `fastonedrive:/search/termo` ou `/p/<id>/search/` (`docs/ARCHITECTURE.md`).
2. Vazio: kdialog cancelado ou termo vazio levanta erro (`kioinstall.py:40`). Sem dolphin, levanta erro (`kioinstall.py:49`).
3. Identidade: o perfil é o activo depois do kdialog (`kioinstall.py:42`). O termo vai percent-encoded (`kioinstall.py:46`).
4. Outro processo: o Dolphin é outro processo e lê o URL.
5. Falha a meio: não grava configuração. Se o dolphin sai com código diferente de zero, levanta erro (`kioinstall.py:55`).
6. Ciclo seguinte: outra busca abre outro Dolphin.
7. Quem chama: CLI `dolphin-search` (`cli.py:559`).

### `listDir` da busca (`kio/kio_fastonedrive.cpp:569`)

1. Especificação: `fastonedrive:/search/termo` lista o SQLite. Não emite `.`.
2. Vazio: termo vazio falha (`kio/kio_fastonedrive.cpp:571`).
3. Identidade: dois resultados com o mesmo nome ficam com sufixo no `UDS_NAME`; o `UDS_URL` continua o do `path` (`kio/kio_fastonedrive.cpp:594`, `kio/kio_fastonedrive.cpp:603`). `localPath` do cache completo e sujo ganha ao pin (`search.py:164`). Cache parcial não entra.
4. Outro processo: `m_itemCache` é deste worker (`kio/kio_fastonedrive.cpp:517`).
5. Falha a meio: erro do CLI falha a listagem (`kio/kio_fastonedrive.cpp:587`). O índice não muda.
6. Ciclo seguinte: outra listagem volta a correr `search`.
7. Quem chama: o Dolphin, no URL de busca.

## Releitura desta passagem

Corrigidos e relidos: `rowToDict`, `listEntries`, `_endDashPositionals` e `listDir` da busca. O resultado da busca usa o cache completo sujo como `localPath`. Um termo `-2024` continua a ir depois de `--`. A releitura não acrescentou achado em aberto.

## Partilhar

Funções que mudam o remoto: `linkRemote`, `_cmdLink`, `_linkOne`.

### `linkRemote` (`rclone.py:483`) e `_cmdLink` (`cli.py:948`)

1. Especificação: Partilhar cria a ligação com `rclone link` (`docs/ROADMAP.md`). O job leva `--exclude` (`docs/ARCHITECTURE.md`). `link` não está em `_FILE_ONLY_OPS` (`rclone.py:817`), por isso o exclude aplica-se.
2. Vazio: sem linhas úteis no stdout, levanta erro de URL (`rclone.py:494`). Caminho vazio falha em `toRemotePath` antes do rclone.
3. Identidade: cada origem é um remote (`cli.py:929`). A URL é a última linha não vazia (`rclone.py:493`).
4. Outro processo: o rclone cria a ligação na conta do remote escrito na argv. O perfil activo pode mudar entre origens; a string do remote é a dessa origem.
5. Falha a meio: uma origem que falha não chega às seguintes (`cli.py:927`). A ligação já criada na origem anterior mantém-se na nuvem.
6. Ciclo seguinte: outra partilha pede outra ligação.
7. Quem chama: CLI `link` e o menu `share` (`dolphin/servicemenu.py:35`). `_linkOne` (`cli.py:942`) é o caminho com `--notify`.

## Releitura desta passagem

Corrigido e relido: `linkRemote` e `_cmdLink`. stdout só com espaços deixa de levantar `IndexError`. Cada origem activa o perfil do seu caminho antes do `rclone link`. A releitura não acrescentou achado em aberto.

## Actividade da bandeja

Funções que mudam disco lido por outro processo: `setActivity`. `readActivity` não grava.

### `setActivity` (`activity.py:9`) e `readActivity` (`activity.py:22`)

1. Especificação: a bandeja mostra a actividade de sync (`docs/ARCHITECTURE.md`).
2. Vazio: ficheiro ausente lê-se como inactivo (`activity.py:25`). JSON inválido também (`activity.py:28`).
3. Identidade: o ficheiro é `activity.json` do perfil activo (`config.py:283`, `config.py:244`).
4. Outro processo: o sync escreve e a janela de definições lê, de 2 em 2 s (`settings.py:1482`). A troca do ficheiro é um `replace` (`activity.py:16`). Quem lê vê o JSON anterior ou o novo.
5. Falha a meio: se o temporário falha, o JSON anterior fica (`activity.py:18`).
6. Ciclo seguinte: o fim do sync grava `idle` (`syncpins.py:115`). Uma excepção grava `error` (`syncpins.py:117`). Cada envio volta a gravar `syncing` (`syncpins.py:890`).
7. Quem chama: `_syncPinsLocked` (`syncpins.py:105`), `_uploadLocal` (`syncpins.py:890`) e `_downloadLocked` (`syncpins.py:971`). Quem lê: `refreshTip` (`settings.py:1466`).

## Releitura desta passagem

Corrigido e relido: `setActivity` e `readActivity`. A bandeja deixa de poder ler um JSON a meio da escrita. A releitura não acrescentou achado em aberto.

## Releitura desta passagem

`index.py`, `search.py`, `cli.py`, `mutate.py`, `copy.py` e `syncpins.py` mudaram depois da leitura anterior. Foram corrigidas e relidas as funções abaixo. Os outros fluxos marcados `por reler` ainda não repetiram o questionário inteiro.

### `_migrateBaselineTokens` (`index.py:1631`)

1. Especificação: baseline vazia não vira `migrated:`; `mt:` é token de conteúdo e continua a decidir o delete do remoto quando o pin local falta (decisão 22). `isContentToken` deixa `qxh:`, `sha1:` e `mt:` (`index.py:1649`).
2. Vazio: `base == ""` volta sem gravar (`index.py:1649`).
3. Identidade: uma linha por `path`. Pasta com `migrated:` perde essa baseline (`index.py:1643`).
4. Outro processo: o `commit` é o de `_migrateSchema` (`index.py:1628`). O sync lê o SQLite depois.
5. Falha a meio: linhas já actualizadas no mesmo `commit` voltam atrás se a transacção não chega ao fim.
6. Ciclo seguinte: `_rebuild` volta a chamar `_migrateSchema` (`index.py:1728`). `mt:` já não é reescrito.
7. Quem chama: `_migrateSchema` (`index.py:1620`), chamado por `_rebuild` (`index.py:1728`) e por `_migrateSchemaIfNeeded` (`index.py:1580`).

### `_intactLocalFile` (`copy.py:83`) e `copyDriveToDrive` (`copy.py:40`)

1. Especificação: cópia dentro do Drive com bytes locais íntegros faz write-back desses bytes (`docs/ARCHITECTURE.md`). Cache completo e sujo, se for o mais recente, ganha ao pin (`copy.py:102`). Sparse não conta (`copy.py:94`).
2. Vazio: caminho vazio devolve `None` (`copy.py:85`). Sem pin e sem cache completo, a cópia segue `copyRemoteToRemote` (`copy.py:71`).
3. Identidade: um `itemPath`. Os dois ficheiros no mesmo caminho comparam o `mtime` (`copy.py:103`).
4. Outro processo: não grava. `isCacheDirty` só corre quando já há linha no índice (`copy.py:97`), sem `stat` remoto extra.
5. Falha a meio: não altera disco. A falha do `stageLocalWrite` fica nesse fluxo.
6. Ciclo seguinte: outra cópia volta a escolher o ficheiro local.
7. Quem chama: `copyDriveToDrive` (`copy.py:52`). Quem chama este: o CLI `copy` entre dois caminhos do Drive.

### `removeRemotePath` (`mutate.py:377`), `_moveAcrossProfiles` (`mutate.py:471`) e `unlink` (`fuse.py:395`)

1. Especificação: Delete apaga a nuvem. Se a cópia local não sai, a linha do índice fica, para o stat não a reenviar. Com a nuvem já ausente e a cópia ainda no disco, o Delete falha e a cópia fica (`mutate.py:386`).
2. Vazio: caminho vazio falha em `_assertSafeItemPath` (`mutate.py:380`).
3. Identidade: um caminho. O move entre perfis limpa a origem no perfil da origem (`mutate.py:500`).
4. Outro processo: `cacheJobsLock` em `removeRemotePath` e no move (`mutate.py:381`, `mutate.py:478`).
5. Falha a meio: a nuvem pode já ter saído e a cópia local ter ficado. A linha só sai se a cópia já não está (`mutate.py:401`, `mutate.py:502`). No `unlink`, um `unlink` falhado não chega a `deleteIndexTree` (`fuse.py:415`).
6. Ciclo seguinte: stat com linha e cópia local não adopta o ficheiro como novo. O sync de um pin já sem remoto e sem alteração local tira a cópia que ficou.
7. Quem chama: CLI `rm`; `unlink` (`fuse.py:404`); `moveRemotePath` (`mutate.py:432`).

A releitura destas funções não mostrou outro efeito da correcção. Não ficou achado `em aberto`. Os fluxos `por reler` da tabela ainda não têm o questionário repetido.

