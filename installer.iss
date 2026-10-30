; MultiZip installer (Inno Setup 6). No admin rights needed.
[Setup]
AppName=MultiZip
AppVersion=1.0
DefaultDirName={autopf}\MultiZip
DefaultGroupName=MultiZip
PrivilegesRequired=lowest
OutputDir=Output
OutputBaseFilename=MultiZip-Setup
Compression=lzma2
ChangesAssociations=yes
UninstallDisplayIcon={app}\MultiZip.exe

[Files]
Source: "dist\MultiZip.exe"; DestDir: "{app}"; Flags: ignoreversion
; licences: MultiZip's own, and those of what the program bundles (7-Zip's must ship with it)
Source: "LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"
Source: "THIRD-PARTY-NOTICES.md"; DestDir: "{app}"
Source: "licenses\7-Zip-License.txt"; DestDir: "{app}\licenses"

[Tasks]
Name: "ctxmenu"; Description: "Add MultiZip to the right-click menu (Extract Here, Add to archive...)"
Name: "assoc"; Description: "Open .zip .7z .rar .tar .gz .zst ... files with MultiZip when double-clicked"
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Icons]
Name: "{group}\MultiZip"; Filename: "{app}\MultiZip.exe"
Name: "{autodesktop}\MultiZip"; Filename: "{app}\MultiZip.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\MultiZip.exe"; Description: "Launch MultiZip"; Flags: postinstall nowait skipifsilent

[Code]
const
  AssocExts = '.zip,.7z,.rar,.tar,.gz,.tgz,.bz2,.tbz2,.xz,.txz,.zst,.tzst';
  CtxExts = '.zip,.7z,.rar,.tar,.gz,.tgz,.bz2,.tbz2,.xz,.txz,.zst,.tzst,.lzma,.iso,.cab,.wim,.swm,.esd,.vhd,.vhdx,.vmdk,.vdi,.qcow2,.dmg,.arj,.lzh,.lha,.z,.taz,.tlz,.cpio,.rpm,.deb,.xar,.squashfs,.cramfs,.chm';

function NextExt(var S: string): string;
var
  P: Integer;
begin
  P := Pos(',', S);
  if P = 0 then
  begin
    Result := S;
    S := '';
  end
  else
  begin
    Result := Copy(S, 1, P - 1);
    Delete(S, 1, P);
  end;
end;

function IsAssoc(E: string): Boolean;
begin
  Result := Pos(',' + E + ',', ',' + AssocExts + ',') > 0;
end;

procedure AddVerb(Key, Verb, Caption, Arg: string);
var
  Base, Exe: string;
begin
  Exe := ExpandConstant('{app}\MultiZip.exe');
  Base := 'Software\Classes\' + Key + '\shell\' + Verb;
  RegWriteStringValue(HKCU, Base, '', Caption);
  RegWriteStringValue(HKCU, Base, 'Icon', Exe);
  RegWriteStringValue(HKCU, Base + '\command', '', '"' + Exe + '" ' + Arg + ' "%1"');
end;

procedure DelKey(Key: string);
begin
  RegDeleteKeyIncludingSubkeys(HKCU, 'Software\Classes\' + Key);
end;

procedure RegisterAll(Ctx, Assoc: Boolean);
var
  S, E, Exe: string;
begin
  Exe := ExpandConstant('{app}\MultiZip.exe');
  if Ctx then
  begin
    AddVerb('*', 'MultiZipAdd', 'MultiZip: Add to archive...', 'a');
    AddVerb('Directory', 'MultiZipAdd', 'MultiZip: Add to archive...', 'a');
  end;
  S := CtxExts;
  while S <> '' do
  begin
    E := NextExt(S);
    if Ctx then
    begin
      AddVerb('SystemFileAssociations\' + E, 'MultiZipXd', 'MultiZip: Extract files...', 'xd');
      AddVerb('SystemFileAssociations\' + E, 'MultiZipXh', 'MultiZip: Extract Here', 'xh');
      AddVerb('SystemFileAssociations\' + E, 'MultiZipX', 'MultiZip: Extract to folder', 'x');
      AddVerb('SystemFileAssociations\' + E, 'MultiZipT', 'MultiZip: Test archive', 't');
    end;
    if Assoc and IsAssoc(E) then
      RegWriteStringValue(HKCU, 'Software\Classes\' + E, '', 'MultiZip.Archive');
  end;
  if Assoc then
  begin
    RegWriteStringValue(HKCU, 'Software\Classes\MultiZip.Archive', '', 'Archive');
    RegWriteStringValue(HKCU, 'Software\Classes\MultiZip.Archive\DefaultIcon', '', Exe + ',0');
    RegWriteStringValue(HKCU, 'Software\Classes\MultiZip.Archive\shell\open\command', '',
      '"' + Exe + '" "%1"');
  end;
end;

procedure UnregisterAll;
var
  S, E, V: string;
begin
  DelKey('*\shell\MultiZipAdd');
  DelKey('Directory\shell\MultiZipAdd');
  S := CtxExts;
  while S <> '' do
  begin
    E := NextExt(S);
    DelKey('SystemFileAssociations\' + E + '\shell\MultiZipXd');
    DelKey('SystemFileAssociations\' + E + '\shell\MultiZipXh');
    DelKey('SystemFileAssociations\' + E + '\shell\MultiZipX');
    DelKey('SystemFileAssociations\' + E + '\shell\MultiZipT');
    V := '';
    // only clear the default MultiZip set: other apps keep their entries (OpenWithProgids ...) under this key
    if RegQueryStringValue(HKCU, 'Software\Classes\' + E, '', V) then
      if V = 'MultiZip.Archive' then
        RegDeleteValue(HKCU, 'Software\Classes\' + E, '');
  end;
  DelKey('MultiZip.Archive');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    RegisterAll(WizardIsTaskSelected('ctxmenu'), WizardIsTaskSelected('assoc'));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    UnregisterAll;
end;
