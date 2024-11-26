import abc
import functools
import mimetypes
import os
import time
from collections import ChainMap
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Generator, Iterator, List, Optional, override

import click
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

    def execute(self, dry_run=False):
        if dry_run:
            return
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

    def make_remove_operation(self):
        return Operation(OperationType.Remove, self.path)

    def make_rename_operation(
        self,
        name_format: str,
        **params,
    ):
        new_name = name_format.format_map(ChainMap(params, self))
        new_path = self.path.parent.joinpath(new_name)
        if self.path != new_path:
            return Operation(OperationType.Rename, self.path, new_path)

    def make_copy_operation(self, dst: Path):
        return Operation(OperationType.Copy, self.path, dst)

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
            if a:
                yield a

    def audio_exists(self, releative=False):
        try:
            next(self.audios(releative=releative))
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


from rich.console import Console
from rich.live import Live
from rich.table import Table


def execute_operations(ops: Iterator[Operation], dry_run: bool):
    console = Console()
    table = Table()
    table.add_column("Operation")
    table.add_column("Input")
    table.add_column("Output")
    with Live(table, console=console, screen=False, auto_refresh=True):
        for op in ops:
            table.add_row(op.op.name, str(op.src), str(op.dst) if op.dst else "")
            op.execute(dry_run=dry_run)


@group
def cli():
    pass


opt_dry_run = option("--dry-run", is_flag=True)
opt_releative = option("--releative", is_flag=True)


@cli.command
@argument("path", type=Path)
@option("--audio-file-format", type=str, default="{tracknumber:>02}-{title}{ext}")
@option("--album-format", type=str, default="{album}")
@opt_releative
@opt_dry_run
def rename(
    path: Path,
    audio_file_format: str,
    album_format: str,
    releative: bool,
    dry_run: bool,
):
    """
    指定したフォルダのオーディオディレクトリを、フォーマットに従いリネームします
    """
    if audio_file_format == "" or album_format == "":
        print("空のフォーマットは指定できません")
        return

    # リネーム
    def operations(base: AudioDirectory) -> Generator[Operation, None, None]:
        for audio in base.audios(releative=releative):
            if op := audio.make_rename_operation(audio_file_format):
                yield op
        for directory in base.directories(releative=releative):
            # ディレクトリ内のアルバム名が一意であればリネーム対象
            albums = set(directory.albums())
            if len(albums) == 1:
                if op := directory.make_rename_operation(
                    album_format, album=albums.pop()
                ):
                    yield op

    # 実行
    execute_operations(operations(AudioDirectory(path)), dry_run)


@cli.command
@argument("src", type=click.Path(exists=True, path_type=Path))
@argument("dst", type=click.Path(path_type=Path))
@opt_releative
@opt_dry_run
def clean_copy(
    src: Path,
    dst: Path,
    releative: bool,
    dry_run: bool,
):
    """
    指定したフォルダのオーディオ関連ファイルのみをコピーします
    """

    def operations(current: AudioDirectory):
        for a in current.audios(releative=releative):
            yield a.make_copy_operation(dst.joinpath(a.path.relative_to(src)))

    execute_operations(operations(AudioDirectory(src)), dry_run)


cli()
