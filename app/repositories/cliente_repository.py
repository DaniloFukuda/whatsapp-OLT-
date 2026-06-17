from sqlalchemy.orm import Session

from app.models.cliente import Cliente


class ClienteRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(self, nome: str, telefone: str) -> Cliente:
        cliente = Cliente(nome=nome, telefone=telefone)
        self.db.add(cliente)
        self.db.commit()
        self.db.refresh(cliente)
        return cliente

    def get_by_phone(self, telefone: str) -> Cliente | None:
        return self.db.query(Cliente).filter(Cliente.telefone == telefone).first()

    def get_or_create(self, nome: str, telefone: str) -> Cliente:
        cliente = self.get_by_phone(telefone)
        if cliente:
            cliente.nome = nome
            self.db.commit()
            self.db.refresh(cliente)
            return cliente
        return self.create(nome=nome, telefone=telefone)
