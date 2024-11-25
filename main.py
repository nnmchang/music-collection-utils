import abc
import functools
import mimetypes
import os
from collections import ChainMap
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Generator, List, Optional, override

import mutagen
from click import argument, group, option


class OperationType(Enum):
    Rename = auto()
    Remove = auto()
    Copy = auto()


@dataclass(frozen=True)
class Operation:
    """
    パス操作クラス
    """

    op: OperationType
    src: Path
    dst: Optional[Path] = None

    def __str__(self) -> str:
        if self.dst:
            return f"{self.op.name}: {self.src} -> {self.dst}"
        else:
            return f"{self.op.name}: {self.src}"

    def execute(self):
        match self.op:
            case OperationType.Rename:
                self.__rename()
            case OperationType.Remove:
                self.__remove()
            case OperationType.Copy:
                self.__copy()

    def __remove(self):
        if self.src.is_dir():
            os.removedirs(self.src)
        else:
            os.remove(self.src)

    def __rename(self):
        if self.dst:
            os.rename(self.src, self.dst)

    def __copy(self):
        if self.dst:
            import shutil

            os.makedirs(self.dst.parent, exist_ok=True)
            shutil.copy2(self.src, self.dst)


@dataclass
class AbstractPath(abc.ABC, dict):
    """
    パス基底クラス
    """

    path: Path

    def format_name(self, format: str, **param):
        return format.format(self, **param)

    def exists(self):
        self.path.exists()

    def remove(self):
        return Operation(OperationType.Remove, self.path)

    def rename(
        self,
        name_format: str,
        **params,
    ):
        new_name = name_format.format_map(ChainMap(params, self))
        new_path = self.path.parent.joinpath(new_name)
        if self.path != new_path:
            return Operation(OperationType.Rename, self.path, new_path)

    def __getitem__(self, key):
        if attr := getattr(self, key):
            return attr

        return super().__getitem__(key)


@dataclass
class FileBase(AbstractPath):
    mime_type: str


@dataclass
class AudioTypeFile(FileBase):
    pass


@dataclass
class AudioMediaFile(AudioTypeFile):
    """
    オーディオ系ファイル
    """

    tag: Optional[mutagen.FileType]

    def get_tag(self, key: str) -> Optional[str]:
        if album := self.tag.get(key):
            return album[0]
        return None

    @property
    def ext(self):
        return mimetypes.guess_extension(self.mime_type)

    @property
    def album(self):
        return self.get_tag("album")

    @property
    def albumartist(self):
        return self.get_tag("albumartist")

    @property
    def title(self):
        return self.get_tag("title")

    @property
    def tracknumber(self):
        return self.get_tag("tracknumber")


@dataclass
class PlaylistFile(AudioTypeFile):
    """
    オーディオ/プレイリスト系ファイル
    """

    @override
    def rename(self, name_format: str, **params):
        return None


@dataclass
class OtherTypeFile(FileBase):
    """
    非オーディオファイル
    """


def load_required(func):
    """
    AudioDirectoryのloadを呼び出すデコレータ
    """

    @functools.wraps(func)
    def wrapper(self: "AudioDirectory", *args, **keargs):
        self.load()
        return func(self, *args, **keargs)

    return wrapper


@dataclass
class AudioDirectory(AbstractPath):
    """
    ディレクトリ
    """

    __initilized: bool = field(default=False, init=False)
    __audios: List[AudioTypeFile] = field(default_factory=list, init=False)
    __other: List[OtherTypeFile] = field(default_factory=list, init=False)
    __childs: List["AudioDirectory"] = field(default_factory=list, init=False)

    def load(self, reload=False):
        """
        ディレクトリを読み込み、各ファイル情報を更新
        """
        if self.path.exists() and not self.__initilized or reload:
            self.__initilized = True
            for path in self.path.iterdir():
                if path.is_dir():
                    self.__childs.append(AudioDirectory(path))
                else:
                    mime, _ = mimetypes.guess_type(path)
                    if mime and mime.startswith("audio/"):
                        if tag := mutagen.File(path):
                            self.__audios.append(AudioMediaFile(path, mime, tag))
                        else:
                            self.__audios.append(PlaylistFile(path, mime))
                    else:
                        self.__other.append(
                            OtherTypeFile(path, mime or "application/octet-stream")
                        )

    @load_required
    def audios(self, releative=False) -> Generator[AudioTypeFile, None, None]:
        if releative:
            for c in self.directories():
                yield from c.audios(True)
        yield from self.__audios

    def albums(self, releative=False):
        for a in (
            a.album
            for a in self.audios(releative=releative)
            if isinstance(a, AudioMediaFile)
        ):
            yield a

    def audio_exists(self):
        try:
            next(self.audios())
            return True
        except StopIteration:
            pass
        return False

    @load_required
    def others(self, releative=False) -> Generator[OtherTypeFile, None, None]:
        if releative:
            for c in self.directories():
                yield from c.others(True)
        yield from self.__other

    @load_required
    def directories(self, releative=False) -> Generator["AudioDirectory", None, None]:
        if releative:
            for c in self.directories():
                yield from c.directories(True)
        yield from self.__childs


@group
def cli():
    pass


@cli.command
@argument("path", type=Path)
@option("--dry-run", is_flag=True)
def cleanup(path: Path, dry_run: bool = False):

    # オーディオ関連ファイル以外を削除
    def remove_others(base: AudioDirectory):
        yield from map(lambda o: o.remove(), base.others(releative=True))

    # オーディオファイルが存在しないディレクトリを削除
    def remove_empty_directories(
        base: AudioDirectory, is_first=True
    ) -> Generator[Operation, None, None]:
        for sub in base.directories():
            yield from remove_empty_directories(sub, is_first=False)
        if (
            # 作業ルートは除外
            not is_first
            # 存在するパスを対象
            and base.path.exists()
            # オーディオファイルが存在しないフォルダを対象
            and not base.audio_exists()
        ):
            yield base.remove()

    # リネーム
    def renames(base: AudioDirectory) -> Generator[Operation, None, None]:
        for audio in base.audios(releative=True):
            if op := audio.rename("{tracknumber:>02}-{title}{ext}"):
                yield op
        for directory in base.directories(releative=True):
            # ディレクトリ内のアルバム名が一意であればリネーム対象
            albums = set(base.albums())
            if len(albums) == 1:
                if op := directory.rename("{album}", album=albums.pop()):
                    yield op

    # オペレーション作成
    def operations(base: Path):
        ad = AudioDirectory(base)
        yield from remove_others(ad)
        yield from remove_empty_directories(ad)
        yield from renames(ad)

    # 実行
    for op in operations(path):
        print(op)
        if not dry_run:
            op.execute()


@cli.command
@argument("src", type=Path)
@argument("dst", type=Path)
@option("--dry-run", is_flag=True)
def copy(src: Path, dst: Path, dry_run: bool):
    # base = AudioDirectory(src)
    audio_format = "{album}/{tracknumber:>02}-{title}{ext}"

    def operations(base: AudioDirectory):
        for a in base.audios(releative=True):
            match a:
                case AudioMediaFile():
                    yield Operation(
                        OperationType.Copy,
                        a.path,
                        dst.joinpath(audio_format.format_map(a)),
                    )
                case PlaylistFile():
                    yield Operation(
                        OperationType.Copy,
                        a.path,
                        dst.joinpath(a.path.relative_to(src)),
                    )

    for op in operations(AudioDirectory(src)):
        print(op)
        if not dry_run:
            op.execute()


cli()
